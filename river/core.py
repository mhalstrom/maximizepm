"""MaximizePM core: storage, graph ordering, claims, registry, capacity.

Every public function takes an open connection from `connect()` and returns
plain dicts and lists, so the CLI and the web server share one code path.
"""

from __future__ import annotations

import contextvars
import json
import math
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
# MaximizePM writes a low one (a ship request notice to an owner that is not the manager); MaximizePM's notice of
# level now (WAKE_LEVEL: a ship request, a review passed, a deploy done) wakes it at once. A blocked message wakes the watch at once: the sender
# says so (--blocked: its item cannot move until the answer), or MaximizePM sees it (BLOCKED_SIGNS). A person's
# message always wakes it at once. On 2026-10-06 to 08, the sender went on with its item after 78 of 128 alerts
# and questions to the manager (current_project/research/worker-stops.md, #1597).
MESSAGE_LEVELS = ("high", "low")
DEFAULT_LEVEL = "high"
WAKE_LEVEL = "now"  # river's own notice that wakes the manager's watch at once (_wake_manager); no sender picks it
# What messages.blocked holds: who said that the message's item stands still. sender: the flag --blocked. waits:
# the sender then ran maxpm inbox --wait. item: the sender released or blocked the item near the send.
BLOCKED_SIGNS = ("sender", "waits", "item")
# MaximizePM marks an alert or a question to a manager as blocked when its sign comes this long before or after
# the send (#1597: one of the two signs showed 39 of the 50 messages whose item stood still).
BLOCKED_WINDOW = timedelta(minutes=3)
# New findings of these kinds wait manage_wait_low: an agent that waits ends by itself after wait_max, and a
# person gets a notification for an item that is ready for them, and the closer decides the items its close
# check listed (unneeded, #3286). A goal with nothing open waits for nobody (goal, #4970).
LOW_FINDINGS = ("waiting", "human", "unneeded", "goal")

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
    # maxpm add first looks for the same work (#2257): open items of the project, and items done in the last
    # similar_days, whose score (0 to 1, words weighted by how rare they are) reaches similar_min. Then it adds
    # nothing and shows them; maxpm add ... --new "<how it differs>" adds anyway. off: no check. maxpm duplicates
    # groups open items with the same score.
    "similar_min": "0.55",
    "similar_days": "14d",
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
    # Where a tap on a notification opens (#4673): maxpm (the MaximizePM page: the item's drawer, the session in
    # the Sessions tab for its question or prompt, the Board's Needs you for a batch) or agent (the agent's own app:
    # Claude Code's claude.ai/code session, Codex's chatgpt.com/codex; else the page). Per person: --agent <person>.
    "notify_open": "maxpm",
    # Where a phone notification opens when its link is the page on this computer and no relay (maxpm connect)
    # makes the page reachable from the phone.
    "ntfy_click": "https://claude.ai/code",
    # The ntfy priority of each notification: min, low, default, high, or urgent. Below high, the ntfy app on
    # Android shows no pop-over banner (the message goes to the notification drawer only), so high is the default.
    "ntfy_priority": "high",
    "ntfy_topic": "",
    "ntfy_token": "",
    "timezone": "",
    # After maxpm done, a session runs maxpm go again at once (#4994). tree (the default): go gives it the next ready
    # task of its own tree (the open tasks linked to what it held in this session: waits-on and blocks links,
    # found-during, the same goal), and none else; when the tree has none it waits, and other work goes to a new
    # session. on: any ready work. off: it stops and reports after each task. Its own queue comes first in each.
    "auto_continue": "tree",
    # The look of a person at a finished result (a page, a text, a design), for each project (#1668).
    # push_first: the worker pushes, closes the item, and checks the result itself or adds an agent check item; a
    # person item only for a taste or product decision (global policy g1, #5347). ask_first: the worker
    # commits, asks in the queue (maxpm ask), and waits for the yes before the push. Under both, public text, a
    # release to production, a step that deletes data or costs money, and an item that says so are asked first.
    # No project gets push_first in silence (#1682): the setup recommends a value (look_setup) and the person
    # confirms it; a project where nobody chose has ask_first.
    "result_look": "ask_first",
    # Canvases (#4648): the largest version in bytes (a page carries its images and scripts inline), and how
    # many versions of a canvas maxpm keeps (a new version then removes the oldest, but never version 1).
    "canvas_max": "5000000",
    "canvas_versions": "50",
    # What a canvas page may load from the network (#4768; mark's answer 3 of #4650): on (any host), off (nothing:
    # the page carries what it shows inline), or a list of hosts, separated by spaces or commas
    # (cdn.jsdelivr.net cdnjs.cloudflare.com). In every case the page runs in a sandbox of its own origin: it never
    # reads maxpm's page, its token or its data.
    "canvas_network": "on",
    # The Terminal tab (#4803): a plain shell on this computer in the page, for a person (never an agent). On for
    # the page on this computer; through the relay only when web_terminal_relay is on too, because whoever holds
    # the relay sign-in could then run any command here.
    "web_terminal": "on",
    "web_terminal_relay": "off",
    "due_warn_before": "3d",
    # Agents the page can start, as "Label=command" entries separated by ";". The first is the default.
    # "@claude-code" or "@codex" is a launch profile: river builds the command from the platform's options
    # (the settings claude_* and codex_*, below; "@codex sandbox=read-only" sets one for that entry alone).
    # Any other command is custom: {model} and {effort} take the launch dialog's choices, and with no
    # choice the flag before them drops out.
    "launch_agents": "Claude Code=@claude-code",
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
    # A session with no work runs maxpm wait (in Claude Code as a background command, so a typed message reaches it;
    # elsewhere in the foreground; a blocking command costs no tokens), never idle at its prompt: it takes new work
    # when it comes, and ends after wait_max without any (0: end at once); a person's prompt starts that clock again.
    # One maxpm wait call returns after wait_step, below a shell time limit.
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
    # Claude Code and Codex (the "blocked" words of their adapters' DETECT, river/agents): add the words of another
    # CLI or a new version with | (maxpm config set). With this default, each agent's pane is read with its own
    # adapter's words; another value is read for every pane.
    # A match is a prompt only at the end of the screen: under the menu come only a key hint, a frame, or a
    # rule (#4943); reply text that asks a question, and the input box, are no prompt.
    "prompt_pattern": r"Do you (want to|trust)|Would you like to|[Ee]nter to confirm|[❯›]\s*\d+\.\s",
    "prompt_wait": "20s",
    # A Claude Code session that river starts gets hooks (the launch option claude_activity_hooks) that report
    # each tool call, prompt, permission prompt and turn end to river (maxpm report --hook claude, #4288). A session that holds
    # an item and reported nothing for activity_silence, and does not wait in maxpm wait, on a person's answer,
    # or on a background command, alerts the active manager; a permission prompt or a turn end with an item in
    # hand alerts it at once. 0s: no silence alert.
    "activity_silence": "10m",
    # A shell command of a session that matches push_guard (a push, a merge of a pull request, a launch) is refused
    # while the session has an unread alert or holds an item that someone blocked (#5381: the rule 'read the inbox
    # before a push' in text failed ten times). The session reads its inbox, then runs the command again. Add a
    # project's own writes (a test account) with a wider expression for that project. Empty: no check. Claude Code
    # sessions that maxpm starts check it in a hook before each Bash command.
    "push_guard": r"\bgit(\s+(-[Cc]\s+\S+|--?[\w-]+(=\S+)?))*\s+push\b|\bgh\s+pr\s+merge\b|\bmaxpm(\s+(--as\s+\S+|--?[\w-]+))*\s+launch\b(?![^|;&\n]*--dry-run)",
    # An agent that ended its turn at its CLI's prompt runs no river command. maxpm serve never types into its
    # pane (a stale session continues a large context), except the answer to a question it waits for there
    # (server.paste_answers, #5303); it starts fresh sessions instead. An agent is idle at its
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
    # tmux_adopt on: maxpm serve moves an agent session that runs outside tmux (a Terminal tab) into a tmux pane, so
    # the Sessions page can type into it (#4598). It waits until the session's turn has ended and it has written
    # nothing for ADOPT_QUIET, with no prompt, question or background work open; then it ends the old process and
    # resumes the same chat in a pane (claude --resume, codex resume) with the same name, item and context.
    "tmux_adopt": "on",
    # revive_sessions on: maxpm serve resumes a session whose process ended with no stop, End or unregister (a crash,
    # a closed window or pane) in a tmux pane at once (claude --resume, codex resume): the same name, model, item and
    # chat, and the manager gets a notice (#5061). Only a session that holds an item, a goal or a target, or the
    # manager, and only when it ran a river command or a hook in the last revive_within.
    "revive_sessions": "on",
    "revive_within": "2h",
    "idle_end": "15m",
    # An expert session (maxpm expert start|adopt, #4955) is one that a person talks with for hours or days: no wait END,
    # no idle_end, no hand-over. maxpm serve ends it when nobody typed or sent it a prompt for expert_idle_end
    # (0s: never), and tells the senders of what it did not read.
    "expert_idle_end": "7d",
    # The prompt cache of an expert (#5023): a person talks with an expert for hours, and after a pause the model reads
    # its whole context again at the cache write price, not the read price. The Sessions tab shows each expert's cache
    # (warm, with the minutes left, or cold) and what the next message costs. When it is cold and its context holds
    # expert_fresh_min tokens or more, the page offers a fresh expert that reads only the old one's handoff.
    # cache_ttl: how long a cache entry lives when the transcript does not say (Claude Code's says: 1h, or 5m in usage
    # overage; a Codex log does not). model_prices: dollars per million tokens of each model family, as
    # input/cache read/5-minute cache write/1-hour cache write (Anthropic's prices of 2026-11; a model of no family
    # here shows tokens only).
    "expert_fresh_min": "150k",
    "cache_ttl": "5m",
    "model_prices": "fable=8/0.2/10/16, opus=4/0.2/5/8, sonnet=2/0.1/2.5/4, haiku=0.1/0.01/0.125/0.2",
    # The Sessions tab opens 'Waits for you' by itself for a question of the open session (and when a person picks
    # its row) only while the question is younger than question_popup_max (#5001); an older open question stays in
    # the card's count, its 'Answer it' link, and maxpm needs-you, but does not pop up. 0s: every question pops up.
    "question_popup_max": "10m",
    # A question from a session to a person goes to the active manager first (#5307): its watch wakes at once, and it
    # answers the question (maxpm answer) or sends it on to the person (maxpm forward). With no manager action within
    # question_hold, or no active manager, the person gets it (Waits for you, needs-you, phone). 0s: the person at once.
    "question_hold": "10m",
    # maxpm serve closes the tmux panes of sessions that are done every tidy_every, as maxpm view --tidy does; a pane
    # it closes by the queue's word (the CLI is still open) must show the same screen for idle_after first. It also
    # ends the tmux servers the tests left behind with their socket gone (maxpm view --orphans). 0s: never.
    "tidy_every": "20m",
    # maxpm serve starts again by itself when the river code on disk is newer than the code it runs (a commit, a
    # pull), so no person restarts it: it looks every notify_interval, and starts again when the files stayed the
    # same for one pass, git shows no change in the code folder that is not committed (an agent in the middle of an
    # edit), and the new code loads. maxpm serve --restart does it at once. off: only a person restarts it.
    "serve_reload": "on",
    # While this computer is connected to the relay (maxpm connect), maxpm serve keeps it from idle sleep
    # (macOS caffeinate -i), so the tunnel answers while the person is away. The display still sleeps; the cost
    # is the power of a computer that stays on (a few watts for a Mac mini or a MacBook on its charger). off:
    # the computer sleeps as usual, and the tunnel is offline while it sleeps.
    "relay_keep_awake": "on",
    # A lease does not run out while its agent is busy. An agent is busy when a command runs in its session (a
    # process below the agent CLI that started after the agent's last river command: a test run, a build), when its
    # tmux pane changes, when its pane shows a prompt for a person, or when its Claude Code transcript has a
    # background command or subagent that has not ended (for busy_max after its start; such a session also keeps
    # its item when news comes for it, #1611). maxpm serve looks every notify_interval and
    # renews the leases of a busy agent; a river command that finds a lease past its time renews it when a command
    # runs in the holder's session. busy_max: how long after an agent's last river command these signs still
    # count (a server that an agent left running would hold an item for ever). 0s: only river commands renew.
    "busy_max": "4h",
    # maxpm serve records the state of each live session (working, waiting_person, waiting_work, manager, expert)
    # in session_samples at most once per sample_every, so the share of session time spent idle has a history
    # (#5352, decision 5 of the canvas does-maxpm-work). 0s: no samples. sample_keep: rows older than this go.
    "sample_every": "1m",
    "sample_keep": "90d",
    # maxpm health (#5353): the person's minutes that one needs-you row stands for. The headline 'person minutes per
    # done item' is needs-you rows x this / items done: a proxy, since MaximizePM cannot see the person's real time.
    "health_person_minutes": "3",
    "manage_every": "30m",
    # maxpm manage --watch waits this long after the first new finding, and then returns all new findings at
    # once: they often come in groups (an agent ends, then its lease runs out), and each wake makes the manager
    # read its whole context again (#1312). A finding that goes away in this time wakes nothing. Messages and a
    # stop request still return at once. 0s: return at the first finding.
    "manage_settle": "2m",
    # How long a message to the manager of the level high or low waits before it wakes maxpm manage --watch
    # (#1583, #1608). A message is high (also an alert and a question) unless the sender gives --level low; a
    # ship request, a review passed and a deploy done wake it at once (#4289). A blocked message (--blocked, or MaximizePM sees that its item stands still)
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
    "manager_autocompact": "250k",
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
    # The deploy hook of a target (maxpm target hook <target> deploy) is its release command: maxpm serve stops it
    # at deploy_timeout instead of hook_timeout, and the release then starts again as after a failure (#4971).
    "deploy_timeout": "2h",
    # A person's list gets an operational cleanup task (maxpm serve, #4973) when it holds list_cleanup_min open items
    # older than list_cleanup_after; at most one such task for a person in each list_cleanup_after. 0s: never.
    "list_cleanup_after": "7d",
    "list_cleanup_min": "10",
    # A shell command, run in the release review's folder, that prints the messages of the commits a release ships,
    # for example git log --format=%B <last release>..<pinned commit>. Its environment has MAXPM_LAST_RELEASE (the
    # output of the target's last done deploy item), MAXPM_TARGET, and MAXPM_REVIEW. Every #<id> it prints is an
    # item of the release: whoever claimed or finished it does not review the release (release_authors), and a done
    # one of a project with this target joins the release review and deploy item (maxpm serve). Empty: only the
    # items sent with maxpm ship count.
    "release_commits": "",
    # Releases move with no manager (maxpm serve, every notify_interval). auto_review on: a ready release review
    # that nobody holds or has reserved goes to a session that waits for work in a project the release ships and
    # held no task before (used_session, release_authors), else to a new session. What serve does for a deploy item is each
    # target's deployer mode (maxpm target deployer). max_sessions: serve starts no session by itself for a release
    # while this many agent sessions are live (0: no limit); a person's Start and maxpm launch do not count it.
    "auto_review": "on",
    "max_sessions": "0",
    # The release gate of a deploy project (#5003, mark: 'This should be a project setting'): review (a release
    # goes out when its review passes; review is on for that project) or person (maxpm serve adds one item for a
    # person to each ready release, and the deploy waits on that yes). Set it on the deploy project (deploy-<target>)
    # or on a project of the target, from the project's needs: maxpm config set release_approval review|person
    # --project <name>. There is no global value: a target where none is set says so in maxpm target show.
    "release_approval": "",
    # Models, weakest to strongest, one list per family ("family: a, b, c", families separated by ";").
    # There is no order across families: a limit compares only models of the same family.
    "model_ladder": "claude: haiku, sonnet, opus, fable; openai: luna, terra, sol, astra",
    "effort_levels": "low, medium, high, xhigh, max",
    # What an item gets when it names none itself. Set them per project (--project) or per item kind
    # (--kind deploy); for example: default_model sonnet, default_effort low, default_max_model sonnet --kind deploy.
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
    # The agent type an item needs, when it names none (maxpm add --agent): any registered agent adapter
    # (claude-code, codex, or a module in river/agents or <queue folder>/agents). agent_rules picks one from the
    # item first: "codex: *.css, *.svg, image, logo" gives Codex the items that touch a .css or .svg file or have
    # the word image or logo in the title. default_agent is the fallback; set it per project (--project) or kind
    # (--kind).
    "agent_rules": "",
    "default_agent": "",
    # The agent each role starts (#5168, docs/agent-adapters.md): agent_for.<role> at the item, project or global
    # scope, before default_agent of the same scope. A worker takes a work item, a reviewer a release review, a
    # deployer a deploy item, an expert an expert-tier task, a context session a goal's base; the manager has no item.
    **{f"agent_for.{_role}": "" for _role in ("worker", "manager", "reviewer", "deployer", "expert", "context")},
}

# The roles that agent_for.<role> names (#5168).
AGENT_ROLES = ("worker", "manager", "reviewer", "deployer", "expert", "context")

# The agents MaximizePM starts, and their options, come from the agent adapters (river/agents, #5169, #5185): see
# launch_platforms() and the end of this module, where the built-in adapters' setting defaults go in DEFAULT_SETTINGS.

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
  monitor           TEXT NOT NULL DEFAULT '',  -- what the deploy session watches after each deploy (maxpm target monitor)
  deployer          TEXT NOT NULL DEFAULT 'launch',  -- what maxpm serve starts for its deploys (maxpm target deployer)
  cadence           TEXT NOT NULL DEFAULT '',  -- how often it releases: 2h, 1d, 1w, 1mo; '' is off (maxpm target cadence)
  cadence_reason    TEXT NOT NULL DEFAULT '',  -- why that cadence fits the project's needs (#5003)
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
  output      TEXT,                      -- the last lines
  result      TEXT                       -- the result as JSON (run_hooks): a command whose hook maxpm serve ran reads it
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
  held_for          TEXT,  -- a person item (doer human) held for the active manager (#5311); NULL: not looked at yet; '': the person has it
  held_until        TEXT,  -- then it goes to the person by itself (question_hold)
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
  tier              TEXT,                     -- 'expert': a task that a person talks through with an expert session (#4959)
  ran_model         TEXT,                     -- the model (ladder name) of the last session that claimed it (#4961)
  backlog_at        TEXT,                     -- in the backlog since then: kept, never offered (maxpm activate ends it)
  backlog_note      TEXT,                     -- why not now
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
  watch_seen     TEXT,                       -- the last poll or return of the manager's watch (maxpm manage --watch)
  busy_at        TEXT,                       -- when maxpm serve last saw the session busy without a maxpm command (keep_busy)
  activity_at     TEXT,                      -- the last state report (maxpm report: its hooks or its pane)
  activity_event  TEXT,                      -- that state: report:working, report:idle, report:blocked, report:gone
  activity_prompt TEXT,                      -- the tool call that waits on a permission prompt now (JSON), if any
  activity_told   TEXT,                      -- the activity_at of the last alert to the manager about it
  activity_waits  TEXT,                      -- at its last turn end: the background work that will wake it (JSON), if any
  report_seq      INTEGER,                   -- the --seq of its last maxpm report, so a late one changes nothing
  report_session  TEXT,                      -- the --session-id of that report
  resume_argv     TEXT,                      -- the command that resumes its session, from maxpm report -- ... (JSON)
  live_settings   TEXT,                      -- settings the page changed in place, {key: {value, base}} (JSON, #5288)
  pid            INTEGER,                    -- the agent CLI process that runs maxpm, its host, and its command line
  host           TEXT,
  pid_cmd        TEXT,
  platform       TEXT,                       -- the agent CLI (a native_message label) and the session's address in it
  native_address TEXT,
  stop_at        TEXT,                       -- maxpm stop: when, who, and why; the session ends after it
  stop_by        TEXT,
  stop_reason    TEXT,
  expert_topic   TEXT,                       -- an expert session (role expert): what it knows and is for (#4955)
  expert_project TEXT,                       -- the project whose folder it starts in (maxpm expert start)
  talked_at      TEXT,                       -- the last prompt that someone typed or sent to it (its hooks)
  assistant_for  TEXT,                       -- an assistant session (role assistant): the person whose list it goes through (#5110)
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
  blocked     TEXT,  -- who said that its item stands still until the answer (BLOCKED_SIGNS); NULL: nobody
  asker_reminded TEXT, -- a question: when the Stop hook reminded its asker to close it (#5001)
  held_for    TEXT,  -- a question to a person: the manager that answers or forwards it first (#5307); NULL: the person has it
  held_until  TEXT   -- then it goes to the person by itself (question_hold)
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

-- The close check (#3286): an open item that the close of closed_id may have made unnecessary, and why.
-- decided_at is set when the closer (or a manager) keeps it; closing the item itself decides it too.
CREATE TABLE IF NOT EXISTS close_checks (
  closed_id   INTEGER NOT NULL REFERENCES items(id),
  item_id     INTEGER NOT NULL REFERENCES items(id),
  why         TEXT NOT NULL,
  found_at    TEXT NOT NULL,
  decided_by  TEXT,
  decided_at  TEXT,
  note        TEXT,
  ready       INTEGER NOT NULL DEFAULT 0,  -- the close made it ready (#3292): relink it, or confirm it
  PRIMARY KEY (closed_id, item_id)
);

-- One row per (event, channel): each event notifies once per channel; a failed send retries.
-- A project's release review, step by step: 'do' is a written instruction the reviewer confirms,
-- 'run' a command that must exit 0 in the project folder. pos orders the steps (1 first).
-- A proposal of the manager to hand a long talk to an expert (#4990): the person starts it with one click in the
-- manager's chat (Continue in an expert), or dismisses it. It holds what maxpm expert start needs.
CREATE TABLE IF NOT EXISTS expert_proposals (
  id          INTEGER PRIMARY KEY,
  manager     TEXT NOT NULL,             -- the session that proposed it: its chat shows the card
  name        TEXT NOT NULL,             -- the expert's name
  topic       TEXT,
  project     TEXT,
  item_id     INTEGER,                   -- the task it takes (--item), else a new expert-tier task with the topic
  model       TEXT,
  model_why   TEXT,
  effort      TEXT,
  summary     TEXT,
  sources     TEXT,                      -- JSON lists
  canvases    TEXT,
  sessions    TEXT,
  state       TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','started','dismissed')),
  created_at  TEXT NOT NULL,
  decided_at  TEXT,
  decided_by  TEXT,
  why         TEXT                       -- a start that failed, or why it was dismissed
);

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

-- A person pinned a session to the top of the worker sessions on the Sessions tab (#5007). The pin goes with the
-- agent: when the session ends and its agent row goes, so does the pin.
CREATE TABLE IF NOT EXISTS session_pins (
  person      TEXT NOT NULL,
  agent       TEXT NOT NULL REFERENCES agents(name) ON DELETE CASCADE ON UPDATE CASCADE,
  pinned_at   TEXT NOT NULL,
  PRIMARY KEY (person, agent)
);

-- The Claude Code sessions (CLAUDE_CODE_SESSION_ID: the transcript's file name) each agent ran commands in.
-- The first prompt of an agent that takes none on its command line (FIRST_PROMPT 'paste', Aider): maxpm serve pastes
-- it into the tmux pane once the agent's idle prompt shows there, then deletes the row (#5221).
CREATE TABLE IF NOT EXISTS pane_prompts (
  pane        TEXT PRIMARY KEY,           -- the tmux pane id
  kind        TEXT NOT NULL,              -- the agent's adapter (an id or alias)
  prompt      TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

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

-- Canvases (#4648): pages that a session makes and maxpm keeps, each version in full, with the comments that
-- people write on a part of a page or on the whole page. One name per project. Private to this computer.
CREATE TABLE IF NOT EXISTS canvases (
  id          INTEGER PRIMARY KEY,
  project_id  INTEGER NOT NULL REFERENCES projects(id),
  name        TEXT NOT NULL,
  title       TEXT,
  kind        TEXT NOT NULL,                -- html, md, svg
  item_id     INTEGER REFERENCES items(id), -- the item it belongs to (its comments go to its holder)
  agent       TEXT,                         -- who put the latest version
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  deleted_at  TEXT,
  UNIQUE (project_id, name)
);
CREATE TABLE IF NOT EXISTS canvas_versions (
  id          INTEGER PRIMARY KEY,
  canvas_id   INTEGER NOT NULL REFERENCES canvases(id),
  version     INTEGER NOT NULL,
  body        TEXT NOT NULL,
  author      TEXT,
  note        TEXT,
  created_at  TEXT NOT NULL,
  UNIQUE (canvas_id, version)
);
CREATE TABLE IF NOT EXISTS canvas_comments (
  id          INTEGER PRIMARY KEY,
  canvas_id   INTEGER NOT NULL REFERENCES canvases(id),
  version     INTEGER NOT NULL,             -- the version it was written on
  parent_id   INTEGER REFERENCES canvas_comments(id),  -- a reply in the thread of this comment
  author      TEXT,
  anchor      TEXT,                         -- JSON {path, text, quote} of the part; NULL: the whole page
  body        TEXT NOT NULL,
  state       TEXT NOT NULL,                -- draft, sent, read, resolved; a reply is 'reply'
  message_id  INTEGER,                      -- the message that carried it to the session
  created_at  TEXT NOT NULL,
  sent_at     TEXT,
  read_at     TEXT,
  resolved_at TEXT,
  resolved_by TEXT,
  resolve_note TEXT
);
CREATE INDEX IF NOT EXISTS canvas_versions_canvas ON canvas_versions(canvas_id, version);
CREATE INDEX IF NOT EXISTS canvas_comments_canvas ON canvas_comments(canvas_id, state);

-- The prompts that people sent to a session from the Sessions tab's message box (#4855): Up and Down in the box
-- bring them back, newest first, on every device and through the relay. At most sessions.PROMPTS_KEEP a session.
CREATE TABLE IF NOT EXISTS sent_prompts (
  id          INTEGER PRIMARY KEY,
  agent       TEXT NOT NULL,
  author      TEXT,
  body        TEXT NOT NULL,
  sent_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sent_prompts_agent ON sent_prompts(agent, id);

-- User policies (#5308): the rules a person sets for the sessions of this queue, so each agent (Claude Code, Codex,
-- any other) reads the ones that apply to it in its briefing. Scope: the whole queue (no project, no role), a project,
-- a role, or a role in a project. Retire deletes the row: no tombstones. The global policies that ship with
-- MaximizePM are in river/policies.json, not here.
CREATE TABLE IF NOT EXISTS policies (
  id          INTEGER PRIMARY KEY,
  project_id  INTEGER REFERENCES projects(id),
  folder      TEXT,                      -- a folder: every session whose folder is in it (#5376)
  role        TEXT,
  text        TEXT NOT NULL,
  source      TEXT NOT NULL,             -- who decided it, and when: 'mark 2026-11-11'
  reason      TEXT NOT NULL,
  items       TEXT,                      -- linked item ids, separated by spaces
  adds_to     TEXT,                      -- the global policy (g<n>) it adds to, if any
  created_by  TEXT,
  created_at  TEXT NOT NULL,
  changed_at  TEXT
);

-- One row per live AI session per sample (#5352): what the session did at that time, so maxpm health can show
-- the share of session time spent idle. secs: the time this row stands for (the gap since the sample before it).
-- The change log of maxpm health (#5354): when a change to the way of work shipped (a feature, a setting, a rule),
-- so maxpm health can show the measures before and after it. A person or the manager adds rows.
CREATE TABLE IF NOT EXISTS changes (
  id          INTEGER PRIMARY KEY,
  at          TEXT NOT NULL,
  name        TEXT NOT NULL,
  note        TEXT,
  actor       TEXT,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_samples (
  id          INTEGER PRIMARY KEY,
  at          TEXT NOT NULL,
  agent       TEXT NOT NULL,
  state       TEXT NOT NULL,             -- working, waiting_person, waiting_work, manager, expert
  role        TEXT,
  item_id     INTEGER,
  project_id  INTEGER,
  secs        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS session_samples_at ON session_samples(at);

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


class SimilarWork(RiverError):
    """maxpm add found the same work in the queue (similar_items) and added nothing. `matches` holds them."""

    def __init__(self, matches):
        self.matches = matches
        lines = ["refused: the queue already has work like this, so nothing was added (similar_min; #2257):"]
        for m in matches:
            when = (m["closed_at"] or "")[:10]
            state = m["status"].replace("_", " ") + (f" by {m['assignee']}" if m["assignee"] and m["status"] in (
                "in_progress", "held") else "") + (f" {when}" if m["status"] == "done" else "")
            lines.append(f"  #{m['id']} [{state}] {m['title']}  (score {m['score']:.2f})")
            if m["output"]:
                out = " ".join(m["output"].split())
                lines.append(f"      output: {out[:200] + '...' if len(out) > 200 else out}")
        first = matches[0]["id"]
        lines += ["The same work: add what you found to it, do not add an item:",
                  f"  maxpm send note \"<what you found>\" --item {first}   (reaches its holder, or its next holder)",
                  f"  or let your item wait on it: maxpm dep <your item> --on {first}",
                  "Different work (a follow-up, another part): add it again with",
                  "  maxpm add ... --new \"<how it differs from #" + str(first) + ">\"   (the history keeps it)"]
        super().__init__("\n".join(lines))


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
    # Every agent starts in a tmux pane now (#5105): the setting that chose a Terminal tab or window is gone.
    conn.execute("DELETE FROM settings WHERE key='launch_in'")
    if "ready" not in {r["name"] for r in conn.execute("PRAGMA table_info(close_checks)")}:
        conn.execute("ALTER TABLE close_checks ADD COLUMN ready INTEGER NOT NULL DEFAULT 0")
    if "result" not in {r["name"] for r in conn.execute("PRAGMA table_info(hook_runs)")}:
        conn.execute("ALTER TABLE hook_runs ADD COLUMN result TEXT")
    if "parent_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(goals)")}:
        conn.execute("ALTER TABLE goals ADD COLUMN parent_id INTEGER REFERENCES goals(id)")
    if "no_goal_context" not in {r["name"] for r in conn.execute("PRAGMA table_info(items)")}:
        conn.execute("ALTER TABLE items ADD COLUMN no_goal_context INTEGER NOT NULL DEFAULT 0")
    if "backlog_at" not in {r["name"] for r in conn.execute("PRAGMA table_info(items)")}:
        conn.execute("ALTER TABLE items ADD COLUMN backlog_at TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN backlog_note TEXT")
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
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(policies)")}
    if "adds_to" not in pcols:
        conn.execute("ALTER TABLE policies ADD COLUMN adds_to TEXT")
    if "folder" not in pcols:
        conn.execute("ALTER TABLE policies ADD COLUMN folder TEXT")
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
    if "wait_token" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN wait_token TEXT")
    if "wait_at" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN wait_at TEXT")
    if "typed_at" not in acols:  # a person typed into the session's pane (#5256): a foreground wait ends
        conn.execute("ALTER TABLE agents ADD COLUMN typed_at TEXT")
    if "waiting_in" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN waiting_in TEXT")
    if "watch_seen" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN watch_seen TEXT")
    for col in ("activity_at", "activity_event", "activity_prompt", "activity_told", "activity_waits"):
        if col not in acols:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT")
    for col, kind in (("report_seq", "INTEGER"), ("report_session", "TEXT"), ("resume_argv", "TEXT"),
                      ("live_settings", "TEXT")):
        if col not in acols:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {kind}")
    if "session_url" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN session_url TEXT")
    if "via" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN via TEXT")
    if "model" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN model TEXT")
    if "agent_type" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN agent_type TEXT")
    for col in ("expert_topic", "expert_project", "talked_at", "assistant_for"):
        if col not in acols:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT")
    for col in ("stop_at", "stop_by", "stop_reason", "platform", "native_address", "host", "pid_cmd", "manage_seen", "busy_at"):
        if col not in acols:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT")
    if "pid" not in acols:
        conn.execute("ALTER TABLE agents ADD COLUMN pid INTEGER")
    # What the session works on and does now, in its own short words (maxpm now, #5086).
    for col in ("headline", "headline_at", "doing", "doing_at"):
        if col not in acols:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT")
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
    if "tier" not in icols:
        conn.execute("ALTER TABLE items ADD COLUMN tier TEXT")
    if "short" not in icols:  # the short title the pages show (#5086)
        conn.execute("ALTER TABLE items ADD COLUMN short TEXT")
    if "ran_model" not in icols:
        conn.execute("ALTER TABLE items ADD COLUMN ran_model TEXT")
    if "native_status" not in {r["name"] for r in conn.execute("PRAGMA table_info(queue_entries)")}:
        conn.execute("ALTER TABLE queue_entries ADD COLUMN native_status TEXT")
    if "native_status" not in {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}:
        conn.execute("ALTER TABLE messages ADD COLUMN native_status TEXT")
    if "asker_reminded" not in {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}:
        conn.execute("ALTER TABLE messages ADD COLUMN asker_reminded TEXT")
    if "level" not in {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}:
        conn.execute("ALTER TABLE messages ADD COLUMN level TEXT")
    if "blocked" not in {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}:
        conn.execute("ALTER TABLE messages ADD COLUMN blocked TEXT")
    if "held_for" not in {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}:
        conn.execute("ALTER TABLE messages ADD COLUMN held_for TEXT")
        conn.execute("ALTER TABLE messages ADD COLUMN held_until TEXT")
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
    if "cadence_reason" not in {r["name"] for r in conn.execute("PRAGMA table_info(targets)")}:
        conn.execute("ALTER TABLE targets ADD COLUMN cadence_reason TEXT NOT NULL DEFAULT ''")
    if "owner_lease" not in {r["name"] for r in conn.execute("PRAGMA table_info(goals)")}:
        conn.execute("ALTER TABLE goals ADD COLUMN owner_lease TEXT")
    if "shared" not in {r["name"] for r in conn.execute("PRAGMA table_info(goals)")}:
        conn.execute("ALTER TABLE goals ADD COLUMN shared INTEGER NOT NULL DEFAULT 0")
    if "held_for" not in {r["name"] for r in conn.execute("PRAGMA table_info(items)")}:
        conn.execute("ALTER TABLE items ADD COLUMN held_for TEXT")
        conn.execute("ALTER TABLE items ADD COLUMN held_until TEXT")
        # Person items that are ready now are the person's already: no hold for them (#5311).
        conn.execute("UPDATE items SET held_for='' WHERE doer='human'")
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
    default = DEFAULT_SETTINGS[key] if key in DEFAULT_SETTINGS else setting_default(key)
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
    return default


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
        setting_default(key)  # an agent's setting (#5169), else RiverError
    if key.endswith(("_ttl", "_after", "_before", "_interval", "_window")) or key in (
            "wait_max", "wait_step", "human_wait_max", "goal_lease", "wait_too_long", "manage_every", "manage_settle", "manage_wait_high", "manage_wait_low", "connect_within",
            "prompt_wait", "idle_end", "expert_idle_end", "revive_within", "question_popup_max", "question_hold", "tidy_every", "busy_max", "sample_every", "sample_keep", "review_timeout", "hook_timeout", "deploy_timeout", "similar_days"):
        parse_duration(value)
    elif key == "expert_fresh_min":
        _k_tokens(value)
    elif key == "model_prices":
        parse_prices(value)
    elif key == "similar_min":
        try:
            ok = value == "off" or 0 < float(value) <= 1
        except ValueError:
            ok = False
        if not ok:
            raise RiverError("similar_min is a number above 0 and at most 1 (0.55), or off")
    elif key in ("prompt_pattern", "push_guard"):
        try:
            re.compile(value)
        except re.error as e:
            raise RiverError(f"{key} is a regular expression, and this one has an error: {e}")
    elif key in ("keep_prereq_limit", "replan_threshold", "max_leases", "goal_max_leases", "serve_port", "list_cleanup_min",
                 "max_sessions", "canvas_max", "canvas_versions", "health_person_minutes"):
        if not value.isdigit():
            raise RiverError(f"{key} takes a whole number")
    elif key == "canvas_network":
        canvas_hosts(value)
    elif key == "launch_agents":
        parse_launch_agents(value)
    elif _platform_setting(key):
        _check_option(*_platform_setting(key), value)
    elif key == "model_ladder":
        parse_ladder(value)
    elif key == "agent_rules":
        parse_agent_rules(value)
    elif key in ("default_agent",) + tuple(f"agent_for.{r}" for r in AGENT_ROLES) and value:
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
    elif key == "release_approval" and value not in RELEASE_APPROVALS:
        raise RiverError("release_approval is review (a release goes out when its review passes) or person (a "
                         "person's yes for each release)")
    elif key in ("review", "auto_review") and value not in ("on", "off"):
        raise RiverError(f"{key} is on or off")
    elif key == "setup_done" and value not in ("on", "off"):
        raise RiverError("setup_done is on or off")
    elif key == "auto_continue" and value not in ("tree", "on", "off"):
        raise RiverError("auto_continue is tree, on, or off")
    elif key == "result_look" and value not in ("ask_first", "push_first"):
        raise RiverError("result_look is ask_first or push_first")
    elif key in ("fresh_sessions", "serve_reload", "tmux_adopt", "revive_sessions", "relay_keep_awake", "web_terminal",
                 "web_terminal_relay") and value not in ("on", "off"):
        raise RiverError(f"{key} is on or off")
    elif key == "manager_autocompact":
        autocompact_tokens(value)
    elif key == "default_prerequisite_mode" and value not in ("keep", "release"):
        raise RiverError("default_prerequisite_mode is keep or release")
    elif key == "ntfy_click" and value and not re.match(r"^https?://[^\s/]+", value):
        raise RiverError("ntfy_click is a web link, for example https://claude.ai/code")
    elif key == "notify_open" and value not in ("maxpm", "agent"):
        raise RiverError("notify_open is maxpm (a tap opens the MaximizePM page) or agent (the agent's own app, "
                         "such as claude.ai/code)")
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
    if key == "release_approval" and not sc.startswith("project:"):
        raise RiverError("release_approval is set for each project from its needs, never for all of them: "
                         "maxpm config set release_approval review|person --project <name> (the deploy project "
                         "deploy-<target>, or a project of the target)")
    also = None
    with tx(conn):
        conn.execute("INSERT INTO settings(scope,key,value) VALUES (?,?,?) "
                     "ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value", (sc, key, value))
        _event(conn, None, actor, f"setting {sc} {key}={mask(key, value)}")
        if key == "release_approval" and value == "review":
            # The gate is the review: the project's releases get one (#5003).
            conn.execute("INSERT INTO settings(scope,key,value) VALUES (?,'review','on') "
                         "ON CONFLICT(scope,key) DO UPDATE SET value='on'", (sc,))
            _event(conn, None, actor, f"setting {sc} review=on (release_approval review)")
            also = "review=on"
    return {"scope": sc, "key": key, "value": mask(key, value), **({"also": also} if also else {})}


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
    # The settings of the agents that are not built in (#5169) follow the built-in ones.
    extra = {k: v for k, v in agent_setting_defaults().items() if k not in DEFAULT_SETTINGS}
    return {"defaults": {**DEFAULT_SETTINGS, **extra} if extra else DEFAULT_SETTINGS, "overrides": rows}


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


def _adapters():
    """The agent adapters (river/agents, docs/agent-adapters.md): imported when first used, since they import core."""
    from . import agents
    return agents


def _platform_of(adapter):
    """The platform name of an adapter: the name that settings, launch profiles (@claude-code) and agents.agent_type
    store, its DESCRIBE['platform'] (Claude Code: claude-code), else its id (#5185)."""
    return (adapter.describe.get("platform") or adapter.id) if adapter else None


def _startable(adapter):
    """Whether MaximizePM can start an adapter's agent: it has the launch part (build_command)."""
    return adapter is not None and adapter.supports("launch")


def agent_types():
    """The agent types MaximizePM can start: the platform name of each registered adapter with a launch part
    (claude-code, codex, then the others by id)."""
    ag = _adapters()
    got = ag.load()
    return [_platform_of(got[i]) for i in ag.ids() if _startable(got[i])]


def _agent_platform(value):
    """An agent type from a name: the platform name (_platform_of) of the registered adapter that the id or an alias
    names ('codex', 'claude-code', 'claude', or an adapter module's id), when it has a launch part; else None."""
    a = _adapters().find((value or "").strip().replace(" ", "-"))
    return _platform_of(a) if _startable(a) else None


def platform_spec(platform):
    """What MaximizePM knows of an agent type for a launch: {label, exe, family, prefix, options}, from its adapter's
    DESCRIBE and the setting specs that are config keys (options: {key: {kind, default, choices, text, label}},
    #5169). An agent with no prompt setting gets one (#5168). RiverError for no registered agent type."""
    a = _adapters().find(platform or "")
    if a is None:
        raise RiverError(f"agent {platform!r} is not registered; the agent types are {', '.join(agent_types())}")
    entry = _platform_entry(a.describe, a.options())
    # Every session starts with a first prompt that runs maxpm go (a manager's: manage, server.manage_command).
    entry["options"].setdefault("prompt", {"kind": "text", "default": FIRST_PROMPT, "choices": [],
                                           "text": "the first prompt", "label": "Prompt"})
    return entry


# The first prompt of a session of an adapter with no prompt setting of its own (#5168), as Codex's default.
FIRST_PROMPT = "run maxpm go in this folder and follow the briefing"


def _check_agent_type(conn, value):
    """An item's agent type: a registered agent that MaximizePM can start, or a launch_agents label that runs one."""
    p = _agent_platform(value)
    if p is None:
        for label, cmd in parse_launch_agents(setting(conn, "launch_agents")):
            if label.lower() == value.strip().lower():
                prof = parse_profile(cmd)
                p = prof[0] if prof else _exe_platform(cmd)
    if p is None:
        raise RiverError(f"agent {value!r}: an agent type is one of {', '.join(agent_types())} "
                         f"(or a launch_agents label that runs one)")
    return p


def _exe_adapter(cmd):
    """The adapter whose program a command starts (its first word's file name is the adapter's exe), or None."""
    exe = Path(cmd.split()[0]).name.lower() if cmd.split() else ""
    return next((a for a in _adapters().load().values() if exe and Path(a.describe["exe"]).name.lower() == exe), None)


def _exe_platform(cmd):
    """The agent type a custom launch_agents command runs, by its program (claude-code for claude, codex for codex,
    an adapter's id for its exe)."""
    a = _exe_adapter(cmd)
    return _platform_of(a) if _startable(a) else None


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
                             f"{', '.join(agent_types())}), for example \"codex: *.css, *.svg, image, logo\"")
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
    """(family, position) of a model in the ladder, or (None, None).

    A full CLI id counts as the ladder name that is one of its parts: claude-opus-5-5, claude-opus-5-5[1m],
    us.anthropic.claude-fable-5-2-v1:0, and gpt-6-astra are opus, opus, fable, and astra. Before this, a
    session that ran go --model claude-opus-5-5 had a model with no order, so no min or max limit applied
    to it, and it took an item for fable (#4950). An id with no ladder name in it, or with two, stays unknown."""
    m = (model or "").strip().lower()
    for fam, ms in ladder.items():
        if m in ms:
            return fam, ms.index(m)
    parts = set(re.split(r"[^a-z0-9]+", m))
    hits = [(fam, i) for fam, ms in ladder.items() for i, x in enumerate(ms) if x in parts]
    return hits[0] if len(hits) == 1 else (None, None)


def _check_model_name(value):
    # Full CLI ids pass too: claude-opus-5-5[1m] (the context size) and us.anthropic.claude-opus-5-5-v1:0 (#4950).
    # A multi agent's provider/model text (opencode/big-pickle, openrouter/Qwen/Qwen3-Coder) has '/' and keeps its
    # case: a provider's model id can hold capitals (#5236).
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}(\[[A-Za-z0-9]{1,8}\])?$", value):
        raise RiverError(f"model {value!r}: a model name uses letters, digits, '.', '_', '-', ':', '/', and an end "
                         f"such as [1m]")
    return value if "/" in value else value.lower()


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


def model_check(ladder, model, lo, hi, family=None):
    """Whether a session running `model` may take an item with limits lo/hi (comma lists).

    Returns (allowed, reason). A limit compares only models of the session's family; a limit that names
    no model of that family does not apply, and the reason says so. A model that the ladder does not know
    (not a ladder name, and no full id that holds one) takes no item with a limit: nothing says that it is
    weak or strong enough (#4950).

    family: the model family of the session's agent (its adapter's DESCRIBE['family']), when it is known. A family
    that model_ladder does not list ("multi": OpenCode, Pi and Goose take a free provider/model text) has no order,
    so no limit applies to it (#5236, docs/agent-adapters.md option A). The family decides, not the text: a
    provider/model text such as openrouter/anthropic/claude-opus-5 holds a ladder name, but no order says that the
    provider's model is the same model."""
    if not (lo or hi):
        return True, ""
    if not model:
        return True, ""
    if family and family not in ladder:
        return True, (f"limits min {lo or '-'}, max {hi or '-'} do not apply: model {model} is of the {family} "
                      f"family, which model_ladder does not order")
    fam, pos = _family(ladder, model)
    if fam is None:
        names = ", ".join(m for ms in ladder.values() for m in ms)
        return False, (f"limits min {lo or '-'}, max {hi or '-'} need a model with an order, and model {model} is "
                       f"not in model_ladder ({names}); name your model by its ladder name: "
                       f"maxpm go --model <name>")
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


def ladder_short(ladder, model):
    """The ladder name of a model (claude-fable-5-2 -> fable), or None when the ladder does not know it."""
    fam, pos = _family(ladder, model)
    return ladder[fam][pos] if fam else None


def work_model(conn, it):
    """(ladder name, why) of the model that the work of an item ran on or was meant for, when it is stronger than the
    default model of its project; else (None, None). An expert that takes up that work gets the same model (#4961: an
    expert for a design that a Fable session wrote started on the default Opus); a plain item never does (#4979). The
    sources: the model of the last session that claimed it (ran_model), its
    own model, its own min model, and its own max model when that is above the default max (a raised ceiling: the
    work was meant for that model)."""
    ladder = parse_ladder(setting(conn, "model_ladder"))
    pid = it["project_id"]
    base = _levels(setting(conn, "default_model", project_id=pid) or setting(conn, "default_max_model", project_id=pid))
    dmax = _levels(setting(conn, "default_max_model", project_id=pid))
    keys = it.keys()

    def pos_in(levels, fam):
        return next((_family(ladder, m)[1] for m in levels if _family(ladder, m)[0] == fam), None)
    best = None
    for f, why in (("ran_model", "its last session ran on"), ("model", "its model is"), ("min_model", "it needs at least"),
                   ("max_model", "it allows up to")):
        v = it[f] if f in keys else None
        for m in _levels(v):
            fam, pos = _family(ladder, m)
            if fam is None:
                continue
            ref = pos_in(base, fam)
            if ref is None or pos <= ref:
                continue
            if f == "max_model" and (pos_in(dmax, fam) is None or pos <= pos_in(dmax, fam)):
                continue
            if best is None or pos > best[2]:
                best = (ladder[fam][pos], f"#{it['id']}: {why} {ladder[fam][pos]}", pos)
    return (best[0], best[1]) if best else (None, None)


def _inherit_model(conn, iid, src_id, actor):
    """Inside a tx: a new expert task that continues the work of src_id gets its model when that is stronger than the
    default (work_model), and a max limit that allows it. A plain item never inherits (#4979)."""
    src = _item(conn, src_id)
    m, why = work_model(conn, src)
    if not m:
        return None
    it = _item(conn, iid)
    if it["model"] or it["min_model"] or it["max_model"]:
        return None  # the adder chose the models
    ladder = parse_ladder(setting(conn, "model_ladder"))
    dmax = setting(conn, "default_max_model", project_id=it["project_id"])
    raise_max = bool(dmax) and not model_check(ladder, m, None, dmax)[0]
    conn.execute("UPDATE items SET model=?, max_model=CASE WHEN ? THEN ? ELSE max_model END WHERE id=?",
                 (m, raise_max, m, iid))
    _event(conn, iid, actor, f"model {m}" + (f", max model {m}" if raise_max else "") +
           f": it continues the work of #{src_id} ({why})")
    return m


def session_model(conn, it, family=None, defaults=True):
    """(model, why) for a new session that takes this item: for an expert task, the model of the work it continues
    (work_model) and the model its last session ran on; then its own model, its recommended default (with defaults),
    else its min limit; each one only when the item's limits allow it, and (family) only a model of the agent's
    family. (None, why) when none fits: the agent CLI's own default runs. A plain item never takes the model its
    last session ran on (#4979: a Fable session that took it once does not make the next one Fable)."""
    ladder = parse_ladder(setting(conn, "model_ladder"))
    m = _item_models(it, _project_name(conn, it["project_id"]), _model_defaults(conn), setting(conn, "model_ladder"))
    cands = []
    if "tier" in it.keys() and it["tier"] == "expert":
        w, why = work_model(conn, it)
        cands += [(w, why), (it["ran_model"], f"#{it['id']}: its last session ran on {it['ran_model']}")]
    cands.append((it["model"], f"#{it['id']}'s own model"))
    if defaults and m["model_from"] != "item":
        cands.append((m["model"], f"the default model ({m['model_from']})"))
    cands.append((it["min_model"], f"#{it['id']} needs at least {it['min_model']}"))
    for cand, how in cands:
        for c in _levels(cand):
            fam, pos = _family(ladder, c)
            if fam is None or (family and fam != family):
                continue
            if model_check(ladder, c, m["min_model"], m["max_model"])[0]:
                return ladder[fam][pos], how
    return None, f"#{it['id']} names no model: the agent's own default"


def _model_defaults(conn):
    """The default_* settings by scope, read once for annotate."""
    rows = {}
    for r in conn.execute("SELECT scope, key, value FROM settings WHERE key IN "
                          "('default_model','default_effort','default_min_model','default_max_model','default_agent') "
                          "OR key LIKE 'agent\\_for.%' ESCAPE '\\'"):
        rows.setdefault(r["scope"], {})[r["key"]] = r["value"]
    try:
        rows["_rules"] = parse_agent_rules(setting(conn, "agent_rules"))
    except RiverError:
        rows["_rules"] = []
    return rows


# Built-in defaults per item kind, below every setting.
KIND_MODELS = {"operational": {"model": "sonnet", "effort": "medium"}}
# Kinds whose built-in default comes before the project and global default_* settings (a setting of the kind,
# default_model --kind <kind>, still comes first): an operational task reads lists and logs, and the default
# model of the queue (often the strongest) is more than it needs (#4973).
KIND_FIRST = ("operational",)


def item_role(it):
    """The role (AGENT_ROLES) of the session that takes an item: a review, a deploy, an expert-tier task, else work."""
    keys = it.keys()
    if "tier" in keys and it["tier"] == "expert":
        return "expert"
    return {"review": "reviewer", "deploy": "deployer"}.get(it["kind"] if "kind" in keys else None, "worker")


def _item_models(it, project, defaults, ladder_text=DEFAULT_SETTINGS["model_ladder"]):
    """Effective model, effort, and limits of an item: its own, else the most specific default_* setting,
    else the built-in default of its kind (KIND_MODELS). The agent: its own, else agent_rules, else in each scope
    from the most specific, agent_for.<role> (item_role) and then default_agent (#5168)."""
    scopes = [f"item:{it['id']}", f"kind:{it['kind']}", f"project:{project}", "global"]
    role_key = f"agent_for.{item_role(it)}"
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
            if sc == f"project:{project}" and it["kind"] in KIND_FIRST and KIND_MODELS[it["kind"]].get(f):
                out[f], out[f + "_from"] = KIND_MODELS[it["kind"]][f], f"kind:{it['kind']}"
                break
            if f == "agent" and defaults.get(sc, {}).get(role_key):
                out[f], out[f + "_from"] = defaults[sc][role_key], f"{sc} {role_key}"
                break
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
    the CLI's own variables, which each adapter names (IDENTITY['inside_env']; Codex: CODEX_THREAD_ID; Claude Code:
    CLAUDECODE). Codex first: a Codex started from a Claude Code shell keeps CLAUDECODE (agents.from_env)."""
    t = _agent_platform(env.get("MAXPM_AGENT_TYPE"))
    if t:
        return t
    a = _adapters().from_env({k: v for k, v in env.items() if k != "MAXPM_AGENT_TYPE"})
    return _platform_of(a) if _startable(a) else None


def outside_tmux(env):
    """True when a command runs in an agent CLI (agent_type_from_env) that is not inside a tmux pane. TMUX alone does
    not say it: a Terminal app started from a tmux pane passes TMUX to every tab it opens. tmux sets TERM_PROGRAM
    to tmux in its panes, so another TERM_PROGRAM there (Apple_Terminal, iTerm.app, vscode) means outside (#4599)."""
    if os.name == "nt" or not agent_type_from_env(env) or env.get("MAXPM_CHAT"):
        return False
    return not env.get("TMUX") or env.get("TERM_PROGRAM", "tmux") != "tmux"


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
    if not fam:
        return None
    got = _adapters().load()  # the first agent of that family that MaximizePM can start (claude: Claude Code)
    return next((_platform_of(got[i]) for i in _adapters().ids()
                 if _startable(got[i]) and got[i].describe["family"] == fam), None)


def session_family(conn, name, stype=None):
    """The model family of a session's agent type (its adapter's DESCRIBE['family']), or None when MaximizePM does
    not know the agent type or it is not registered."""
    stype = stype or session_type(conn, name)
    if not stype:
        return None
    a = _adapters().find(stype)
    return a.describe.get("family") if a else None


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


def _project_counts(conn, project_id):
    """The open, in-progress and done items of a project, and who holds the ones in progress."""
    rows = conn.execute("SELECT status, assignee FROM items WHERE project_id=?", (project_id,)).fetchall()
    held = [r for r in rows if r["status"] in ("in_progress", "held")]
    return {"open_items": sum(r["status"] in OPEN_STATES for r in rows), "in_progress": len(held),
            "holders": sorted({r["assignee"] for r in held if r["assignee"]}),
            "done_items": sum(r["status"] == "done" for r in rows)}


def project_archive(conn, name, actor=None):
    """Archive a project (#5197): its items leave the queue and every list, and no session is offered them. Nothing is
    deleted: project_restore brings it back with its items, goals and settings. A session that holds one of its items
    keeps it until it finishes or releases it."""
    with tx(conn):
        p = _project(conn, name)
        name = p["name"]
        if p["archived"]:
            raise RiverError(f"project {name} is archived already; to bring it back: maxpm project restore {name}")
        conn.execute("UPDATE projects SET archived=1, rank=100000 WHERE name=?", (name,))
        # The projects after it move up one place, so the ranks stay 1..n.
        for i, r in enumerate(conn.execute("SELECT name FROM projects WHERE archived=0 ORDER BY rank, id").fetchall(), 1):
            conn.execute("UPDATE projects SET rank=? WHERE name=?", (i, r["name"]))
        _event(conn, None, actor, f"project {name} archived")
    return {"name": name, "archived": True, **_project_counts(conn, p["id"])}


def project_restore(conn, name, actor=None):
    """Bring an archived project back (#5197), as the least important project: its open items are in the queue again.
    `maxpm project rank` moves it up."""
    with tx(conn):
        p = _project(conn, name)
        name = p["name"]
        if not p["archived"]:
            raise RiverError(f"project {name} is not archived; archived projects: maxpm project list --archived")
        top = conn.execute("SELECT COALESCE(MAX(rank),0) m FROM projects WHERE archived=0").fetchone()["m"]
        conn.execute("UPDATE projects SET archived=0, rank=? WHERE name=?", (top + 1, name))
        _event(conn, None, actor, f"project {name} restored")
    return {"name": name, "archived": False, "rank": top + 1, **_project_counts(conn, p["id"])}


def project_archived(conn):
    """The archived projects, last archived first, with their item counts (the Plan tab's Archived list)."""
    out = []
    for r in conn.execute("SELECT id, name, notes, path FROM projects WHERE archived=1").fetchall():
        at = conn.execute("SELECT MAX(at) m FROM events WHERE item_id IS NULL AND (change=? OR change LIKE ?)",
                          (f"project {r['name']} archived", f"project {r['name']} archived (%")).fetchone()["m"]
        out.append({"name": r["name"], "notes": r["notes"], "path": r["path"], "archived_at": at,
                    **_project_counts(conn, r["id"])})
    return sorted(out, key=lambda r: (r["archived_at"] or "", r["name"]), reverse=True)


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
        counts={**{s: sum(1 for a in mine if a["status"] == s and not a["backlog"])
                   for s in ("open", "in_progress", "held", "done", "dropped")},
                "backlog": sum(1 for a in mine if a["backlog"])},
        ready=[{k: v for k, v in a.items() if k != "sort_key"} for a in ready[:5]],
        ready_count=len(ready),
        working_now=holders,
        worked_recently=recent,
        goals=goal_list(conn, name, include_complete=False),
        tracker=setting(conn, "tracker", project_id=p["id"]),
        result_look=setting(conn, "result_look", project_id=p["id"]),
        look=look_setup(conn, name),
    )
    if p.get("target"):  # the release gate and cadence of its target, each set from the project's needs (#5003)
        t = conn.execute("SELECT cadence, cadence_reason FROM targets WHERE name=?", (p["target"],)).fetchone()
        p["release_gate"] = dict(release_approval(conn, p["target"]), cadence=t["cadence"] if t else "",
                                 cadence_reason=t["cadence_reason"] if t else "")
    return p


def project_list(conn):
    rows = [dict(r) for r in conn.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM items i WHERE i.project_id=p.id AND i.status IN ('open','in_progress','held') "
        "AND i.backlog_at IS NULL) open_items, (SELECT COUNT(*) FROM items i WHERE i.project_id=p.id AND i.status='open' "
        "AND i.backlog_at IS NOT NULL) backlog_items FROM projects p WHERE archived=0 ORDER BY rank, id")]
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
    """Give a deploy target a new name, in one transaction: its projects, its deploy and review
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


def _goal_notice(conn, item_id, actor, verb, detail=None, ask=None):
    """Tell each goal's owner when someone else claims or finishes an item tagged with it. ask: goal name ->
    the done-when question for that goal's owner (the item was its last open one), in the same notice."""
    ask = ask or {}
    for g in conn.execute("SELECT g.name, g.owner FROM item_goals ig JOIN goals g ON g.id=ig.goal_id "
                          "WHERE ig.item_id=? AND g.owner IS NOT NULL AND g.status='open'", (item_id,)).fetchall():
        if (actor and g["owner"] != actor) or g["name"] in ask:
            it = _item(conn, item_id)
            _send(conn, "notice", "maxpm", f"{actor or 'someone'} {verb} #{item_id} {it['title']} (your goal {g['name']})"
                  + (f": {detail}" if detail else "")
                  + (f". It was the last open item of the goal. {ask[g['name']]}" if g["name"] in ask else ""),
                  to=g["owner"], item_id=item_id)


def _goal_emptied(conn, g):
    """True when an open goal has no open item that is offered (backlog items do not count) and no open
    sub-goal, after some work: a closed item, or a complete sub-goal. Then its done-when test may hold."""
    if g["status"] != "open":
        return False
    if conn.execute("SELECT 1 FROM item_goals ig JOIN items i ON i.id=ig.item_id WHERE ig.goal_id=? "
                    f"AND i.status IN {OPEN_STATES} AND i.backlog_at IS NULL", (g["id"],)).fetchone():
        return False
    if conn.execute("SELECT 1 FROM goals WHERE parent_id=? AND status='open'", (g["id"],)).fetchone():
        return False
    return bool(conn.execute("SELECT 1 FROM item_goals ig JOIN items i ON i.id=ig.item_id WHERE ig.goal_id=? "
                             f"AND i.status IN {CLOSED_STATES}", (g["id"],)).fetchone()
                or conn.execute("SELECT 1 FROM goals WHERE parent_id=? AND status='complete'", (g["id"],)).fetchone())


def _goal_owner_live(g):
    """The goal's owner while its claim lasts, else None (an owner past goal_lease is no owner)."""
    return g["owner"] if g["owner"] and (g["owner_expires_at"] or "") >= iso(now()) else None


def _ask_done_when(conn, goals, actor, last, send=True):
    """Inside a tx, after the close of a goal's item (or a sub-goal's completion): for each goal that has no
    open item now, ask whether its done-when test holds (#4970). Nobody asked before, so 12 goals stayed open
    with nothing left to do. The owner gets a notice (the closer, when it owns the goal, reads it in the reply);
    a goal with no owner (shared, or its owner's claim ran out) is the manager's: manager_findings lists it
    until someone completes it or adds work. last: what closed, '#12 <title>' or 'sub-goal <name>'.
    send=False: the caller puts each owner's question (in 'question') into its own notice.
    Returns one dict per goal for the reply: goal, done_when, backlog, asked ('you', the owner, or 'manager')."""
    out = []
    for g in goals:
        if not _goal_emptied(conn, g):
            continue
        owner = _goal_owner_live(g)
        backlog = [r["id"] for r in conn.execute(
            "SELECT i.id FROM item_goals ig JOIN items i ON i.id=ig.item_id WHERE ig.goal_id=? AND i.status='open' "
            "AND i.backlog_at IS NOT NULL ORDER BY i.id", (g["id"],))]
        if owner and owner != actor and send:
            _send(conn, "notice", "maxpm", f"{last} was the last open item of your goal {g['name']}. "
                  + _done_when_question(g, backlog), to=owner)
        _event(conn, None, actor, f"goal {g['name']}: no open item after {last}; asked "
               + (f"{owner} if done-when holds" if owner else "the manager if done-when holds (no owner)"))
        out.append({"goal": g["name"], "done_when": g["done_when"], "backlog": backlog,
                    "asked": "you" if owner and owner == actor else owner or "manager",
                    "question": _done_when_question(g, backlog)})
    return out


def _done_when_question(g, backlog=()):
    """The question to whoever decides a goal with no open item: does done-when hold, and the two answers."""
    test = f"Does its done-when hold ({g['done_when']})?" if g["done_when"] else \
        f"Is its outcome reached ({g['outcome'] or 'no outcome written'})?"
    return (test + f" Yes: maxpm goal done {g['name']} --result \"<one line>\""
            + (" --drop-open (or activate the backlog items " + ", ".join(f"#{i}" for i in backlog) + ")"
               if backlog else "")
            + f". No: add the items it still needs: maxpm add \"<title>\" --goal {g['name']}")


def _goal_view(conn, g, ann=None):
    ann = ann if ann is not None else annotate(conn)
    d = dict(g)
    d["project"] = _project_name(conn, g["project_id"])
    ids = [r["item_id"] for r in conn.execute("SELECT item_id FROM item_goals WHERE goal_id=?", (g["id"],))]
    items = [ann[i] for i in ids if i in ann]
    d["items_open"] = sorted(a["id"] for a in items if a["status"] in OPEN_STATES and not a["backlog"])
    d["items_backlog"] = sorted(a["id"] for a in items if a["backlog"])
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
    none): {"id", "title", "closed_at"}; else None. Only an agent owner keeps a handoff. A shared goal has no
    owner: each worker that finishes a task of it brings the handoff up to date (#4974), so the last finished
    item by anyone counts."""
    if g["shared"] and g["status"] == "open":
        h = _handoff(conn, g["id"])
        r = conn.execute("SELECT i.id, i.title, i.closed_at FROM items i JOIN item_goals ig ON ig.item_id=i.id "
                         "WHERE ig.goal_id=? AND i.status='done' AND i.closed_at IS NOT NULL "
                         "ORDER BY i.closed_at DESC, i.id DESC LIMIT 1", (g["id"],)).fetchone()
        return dict(r, shared=True) if r and (not h or r["closed_at"] > h["created_at"]) else None
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
                  for i in sorted(d["items_open"] + d["items_backlog"] + d["items_done"] + d["items_dropped"],
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
    view = _goal_view(conn, g)
    still = view["items_open"] + view["items_backlog"]
    if still and not drop_open:
        raise RiverError(f"goal {name} has open items: {', '.join('#' + str(i) for i in view['items_open']) or 'none'}"
                         + (f"; backlog items: {', '.join('#' + str(i) for i in view['items_backlog'])}"
                            if view["items_backlog"] else "")
                         + f". Finish them, untag them (maxpm edit <id> --untag {name}), or drop them: "
                         f"maxpm goal done {name} --result \"...\" --drop-open")
    for i in still:
        drop(conn, i, actor, note=f"goal {name} completed without it: {result}")
    with tx(conn):
        conn.execute("UPDATE goals SET status='complete', result=?, completed_at=?, owner=NULL, owner_expires_at=NULL "
                     "WHERE id=?", (result.strip(), iso(now()), g["id"]))
        _event(conn, None, actor, f"goal {name} complete: {result.strip()}")
        checks = _ask_done_when(conn, conn.execute("SELECT * FROM goals WHERE id=?", (g["parent_id"],)).fetchall(),
                                actor, f"sub-goal {name}") if g["parent_id"] else []
    res = goal_show(conn, name)
    res["goal_checks"] = checks
    return res


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
    The deploy session reads it in its deploy task: after the deploy it watches the release as the text says
    (#4972: no separate monitor task or session). A target with a deploy hook has no session: its release command
    does the checks, and a failure (exit not 0) starts the release again."""
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
    "deploy": "the release command: maxpm serve runs it when the deploy item is ready (with review on: when the review "
              "passed), and no session takes the deploy item. Exit 0 closes the deploy item; a failure keeps it open, "
              "adds a fix item, and the release starts again: a new cut, a new review, then this hook again",
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


def may_set_hook(conn, actor):
    """Who may set, change, or remove a hook: a person or a manager session, because maxpm serve runs a hook
    outside every sandbox (#1826). None (no name: a person at the terminal, or the page) may. Returns why not."""
    r = conn.execute("SELECT kind, role FROM agents WHERE name=?", (actor,)).fetchone() if actor else None
    if r is None or r["kind"] == "human" or r["role"] == "manager":
        return None
    return (f"{actor} is an agent; only a person or the manager sets, changes, or removes a hook, because maxpm "
            f"serve runs it outside the sandbox. Ask the manager: maxpm --as {actor} ask manager \"<the hook and why>\"")


def target_hook(conn, name, event=None, command=None, clear=False, actor=None):
    """maxpm target hook: set, remove, or list the hooks of a target. A hook is a shell command that MaximizePM
    runs when an event of the target occurs (HOOK_EVENTS), one for each event: a script can call more. The
    command that causes the event runs it (run_hooks), or maxpm serve when that command is in a sandbox. A hook
    never undoes and never refuses its event. Only a person or the manager changes a hook (may_set_hook)."""
    changed = None
    with tx(conn):
        t = _target(conn, name)
        name = t["name"]
        if event is None:
            if clear or command is not None:
                raise RiverError(f"name the event: maxpm target hook {name} <{'|'.join(HOOK_EVENTS)}> \"<command>\"")
        else:
            _hook_event(event)
            if clear or command is not None:
                why = may_set_hook(conn, actor)
                if why:
                    raise RiverError(why)
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
            "timeout": setting(conn, "hook_timeout", project_id=_deploy_project_id(conn, name)),
            "deploy_timeout": setting(conn, "deploy_timeout", project_id=_deploy_project_id(conn, name)),
            "review": setting(conn, "review", project_id=_deploy_project_id(conn, name)),
            "deployer": _target(conn, name)["deployer"]}


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


def _hook(conn, event, target, item_id, actor, deploy_id=None, token=None):
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
                                                rev["cut_rev"] if rev else None, actor, iso(now()), token or hook_token()))


def _review_deploy(conn, review_id):
    """The deploy item that waits on a release review, or None."""
    r = conn.execute("SELECT i.id FROM deps d JOIN items i ON i.id=d.item_id WHERE d.blocked_by=? AND i.kind='deploy' "
                     "ORDER BY i.id LIMIT 1", (review_id,)).fetchone()
    return r["id"] if r else None


def hooks_waiting(conn, token=None):
    """Whether hooks that the events of this command (or of token) queued wait to run."""
    return conn.execute("SELECT 1 FROM hook_runs WHERE started_at IS NULL AND queued_by=? LIMIT 1",
                        (token or hook_token(),)).fetchone() is not None


HOOK_POLL = 0.5  # seconds between two looks at the hook runs that maxpm serve runs for a command (hook_results)
HOOK_TAKE_AFTER = 15  # seconds after which a command runs a hook itself that maxpm serve did not start


def hook_results(conn, token, cwd=None):
    """maxpm serve runs the hooks of the events of a command in a sandbox (#1826): wait until each run of token
    ends and return its result, as run_hooks does. A run that serve did not start within HOOK_TAKE_AFTER (it
    started again meanwhile) runs here, in the sandbox. A run that is not done after its hook_timeout and a
    minute more: its result says that it still runs, and the history of the deploy item gets it later."""
    import time
    q = "SELECT * FROM hook_runs WHERE queued_by=? ORDER BY id"
    rows = conn.execute(q, (token,)).fetchall()
    limit = sum(parse_duration(setting(conn, "hook_timeout", item_id=r["deploy_id"])).total_seconds() for r in rows)
    t0 = time.monotonic()
    out = []
    while True:
        rows = conn.execute(q, (token,)).fetchall()
        if all(r["ended_at"] for r in rows):
            break
        waited = time.monotonic() - t0
        if waited > HOOK_TAKE_AFTER and any(r["started_at"] is None for r in rows):
            out += run_hooks(conn, cwd, token, sandbox=True)
            continue
        if waited > limit + 60:
            break
        time.sleep(HOOK_POLL)
    done = {h["id"] for h in out}
    for r in rows:
        if r["id"] in done:
            continue
        if r["result"]:
            out.append(json.loads(r["result"]))
        else:
            out.append({"id": r["id"], "target": r["target"], "event": r["event"], "command": r["command"],
                        "item": r["item_id"], "deploy": r["deploy_id"], "rev": r["rev"], "folder": None,
                        "exit_code": None, "timed_out": False, "seconds": None, "ok": None,
                        "result": f"still runs in maxpm serve; its result goes to the history of #{r['deploy_id']}",
                        "output": "", "alerted": None, "sandbox": False})
    return sorted(out, key=lambda h: h["id"])


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
    limit_key = "deploy_timeout" if event == "deploy" else "hook_timeout"
    limit_text = setting(conn, limit_key, item_id=run["deploy_id"])
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
    result = (f"stopped at {limit_key} ({limit_text}) after {took}" if timed_out
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
        if not ok and event != "deploy":  # a failed deploy has its own alert (_deploy_hook_end)
            tg = conn.execute("SELECT owner FROM targets WHERE name=?", (target,)).fetchone()
            to = (tg["owner"] if tg and tg["owner"] else None) or active_manager(conn)
            if to and to != actor:
                _send(conn, "alert", "maxpm", f"hook {event} of target {target} failed ({result}) for "
                      f"#{run['item_id']}: {cmd}" + (f"\n{tail[-1500:]}" if tail else "")
                      + f"\nThe event stands: a hook never undoes it. Find the cause; when the release needs the "
                      f"command, run it by hand" + (f" in {where}" if where else "") + "."
                      + (f" {HOOK_SANDBOX}." if sandbox else ""), to=to, item_id=run["deploy_id"])
                out["alerted"] = to
        conn.execute("UPDATE hook_runs SET result=? WHERE id=?", (json.dumps(out), run["id"]))
    if event == "deploy" and run["deploy_id"]:
        out.update(_deploy_hook_end(conn, run, ok, result, tail, where))
        with tx(conn):
            conn.execute("UPDATE hook_runs SET result=? WHERE id=?", (json.dumps(out), run["id"]))
    return out


# ---- the deploy hook (#4971, mark: 'the deploy should be a hook', 'if the target has a deploy plan', 'deploy should
# be triggered. if it goes wrong we fix and start again from the beginning'). A target with a deploy hook releases
# with no session that types its command: maxpm serve runs the hook when the deploy item is ready. While it runs the
# deploy item waits on an outside blocker set by DEPLOY_HOOK_BY, so every reader of the queue sees that it runs.
DEPLOY_HOOK_BY = "maxpm deploy hook"
DEPLOY_HOOK_LOST = 600  # seconds after deploy_timeout when a run that never ended counts as failed


def deploy_hook(conn, target):
    """The deploy hook (the release command) of a target, or None."""
    r = conn.execute("SELECT h.command FROM hooks h JOIN targets t ON t.id=h.target_id WHERE t.name=? "
                     "AND h.event='deploy'", (target,)).fetchone() if target else None
    return r["command"] if r else None


RELEASE_APPROVALS = ("review", "person")
APPROVE_TITLE = "Approve release"


def release_approval(conn, target):
    """The release gate of a target (#5003): {"approval": review|person|"", "project": where it is set}. It is read
    from the deploy project (deploy-<target>), else from the target's projects in rank order. "" when none sets it:
    there is no global default."""
    names = [f"deploy-{target}"] + [r["name"] for r in conn.execute(
        "SELECT name FROM projects WHERE target=? AND archived=0 ORDER BY rank, id", (target,))]
    for n in names:
        r = conn.execute("SELECT value FROM settings WHERE scope=? AND key='release_approval'", (f"project:{n}",)).fetchone()
        if r and r["value"]:
            return {"approval": r["value"], "project": n}
    return {"approval": "", "project": None}


def _approvals(conn, dep_id):
    """The person's approval items of a deploy item, newest first."""
    return conn.execute("SELECT i.* FROM items i JOIN deps d ON d.blocked_by=i.id WHERE d.item_id=? AND i.doer='human' "
                        "AND i.title LIKE ? ORDER BY i.id DESC", (dep_id, APPROVE_TITLE + " %")).fetchall()


def approval_refusal(conn, item):
    """Why a session may not take this deploy item yet, or None: its target takes a person's yes for each release
    (release_approval person) and this release has none."""
    if item["kind"] != "deploy" or release_approval(conn, item["target"])["approval"] != "person":
        return None
    if any(a["status"] == "done" for a in _approvals(conn, item["id"])):
        return None
    return (f"target {item['target']} takes a person's yes for each release (release_approval person): maxpm serve "
            f"asks for it when deploy #{item['id']} is ready, and the deploy waits on it")


def release_yes(conn, dep_id, actor="maxpm"):
    """maxpm serve, for a ready deploy item whose target takes a person's yes (release_approval person) and that
    has not asked for it: one item for a person, which the deploy item waits on. Returns its id, or None."""
    with tx(conn):
        dep = _item(conn, dep_id)
        if dep["kind"] != "deploy" or dep["status"] != "open" or approval_refusal(conn, dep) is None:
            return None
        if any(a["status"] != "dropped" for a in _approvals(conn, dep_id)):
            return None  # asked already: an open item waits for the person
        rows = conn.execute("SELECT i.id, i.title, i.kind, i.status FROM deps d JOIN items i ON i.id=d.blocked_by "
                            "WHERE d.item_id=? AND i.kind<>'deploy' ORDER BY i.id", (dep_id,)).fetchall()
        ships = [r for r in rows if r["kind"] != "review" and r["status"] != "dropped"]
        reviews = [r for r in rows if r["kind"] == "review" and r["status"] == "done"]
        gate = release_approval(conn, dep["target"])
        lines = "\n".join(f"  #{r['id']} {r['title']}" for r in ships) or "  (nothing listed)"
        context = (f"Release of target {dep['target']} (deploy #{dep_id}) is ready and waits for your yes: the "
                   f"project {gate['project']} says release_approval person.\nIt ships:\n{lines}\n"
                   + (f"Its review passed: #{reviews[-1]['id']}.\n" if reviews else "")
                   + f"Yes: maxpm done <this id>; the release then goes out (maxpm serve runs its deploy hook, or "
                   f"the deployer takes deploy #{dep_id}). Not now: leave this item open, and say why on it.")
        top = conn.execute("SELECT COALESCE(MAX(rank),0) m FROM items WHERE project_id=?", (dep["project_id"],)).fetchone()["m"]
        cur = conn.execute("INSERT INTO items(project_id,title,priority,rank,doer,context,kind,target,created_at) "
                           "VALUES (?,?,?,?,'human',?,'work',?,?)",
                           (dep["project_id"], f"{APPROVE_TITLE} {dep['target']}: {len(ships)} item(s), deploy #{dep_id}",
                            dep["priority"], top + 1, context, dep["target"], iso(now())))
        yes = cur.lastrowid
        _event(conn, yes, actor, f"added: a person's yes for the release of deploy #{dep_id} (release_approval person)")
        _dep_add(conn, dep_id, yes, actor, alert=False)
        _event(conn, dep_id, actor, f"waits on #{yes}: a person's yes for this release (release_approval person)")
    sync_needs_you(conn)
    return yes


def release_yes_pass(conn):
    """maxpm serve: each ready deploy item of a target that takes a person's yes asks for it once (release_yes).
    Returns {deploy id: approval item id}."""
    out, ann = {}, None
    for d in conn.execute(f"SELECT * FROM items WHERE kind='deploy' AND status='open' ORDER BY id").fetchall():
        if approval_refusal(conn, d) is None or any(a["status"] != "dropped" for a in _approvals(conn, d["id"])):
            continue
        ann = ann or annotate(conn)
        a = ann.get(d["id"])
        if a is None or not a["ready"] or a["project_archived"]:
            continue
        y = release_yes(conn, d["id"])
        if y:
            out[d["id"]], ann = y, None
    return out


def hooked_targets(conn):
    """The names of the targets that have a deploy hook."""
    return {r["name"] for r in conn.execute(
        "SELECT t.name FROM hooks h JOIN targets t ON t.id=h.target_id WHERE h.event='deploy'")}


def deploy_hook_refusal(conn, item, actor):
    """Why this agent may not take this deploy item, or None: its target has a deploy hook, so maxpm serve runs
    the release command and no session types it. A person may still take it."""
    if item["kind"] != "deploy" or not _is_ai(conn, actor) or not deploy_hook(conn, item["target"]):
        return None
    return (f"target {item['target']} has a deploy hook: maxpm serve runs the release when deploy #{item['id']} is "
            f"ready, and no session types its command (maxpm target hook {item['target']})")


def deploy_hook_start(conn, dep_id, token=None):
    """maxpm serve starts the deploy hook of a ready deploy item: the item waits on an outside blocker while it
    runs, the release is cut when no reviewer cut it, and one run of the hook is queued for token. Returns the run
    id, or None when the item is not open, nobody may start it now, or the target has no deploy hook."""
    with tx(conn):
        dep = _item(conn, dep_id)
        cmd = deploy_hook(conn, dep["target"])
        if (dep["kind"] != "deploy" or dep["status"] != "open" or dep["assignee"] or dep["blocked_reason"]
                or not cmd or approval_refusal(conn, dep)):
            return None
        t = iso(now())
        conn.execute("UPDATE items SET blocked_reason=?, blocked_at=?, blocked_until=NULL, blocked_set_by=? WHERE id=?",
                     (f"the deploy hook of target {dep['target']} runs it (maxpm serve, since {t}); no session "
                      f"takes it", t, DEPLOY_HOOK_BY, dep["id"]))
        _release_cut(conn, dep["id"], "maxpm", "the deploy hook started")
        if token:  # the release-cut hook of that cut runs first, in the same thread as the deploy
            conn.execute("UPDATE hook_runs SET queued_by=? WHERE queued_by=? AND started_at IS NULL AND deploy_id=?",
                         (token, hook_token(), dep["id"]))
        _event(conn, dep["id"], "maxpm", f"deploy hook started by maxpm serve: {cmd}")
        _hook(conn, "deploy", dep["target"], dep["id"], None, token=token)
        run = conn.execute("SELECT MAX(id) FROM hook_runs WHERE deploy_id=? AND event='deploy'", (dep["id"],)).fetchone()[0]
    return run


def deploy_hook_lost(conn):
    """Runs of a deploy hook that started and never ended, deploy_timeout and DEPLOY_HOOK_LOST ago (maxpm serve
    stopped while one ran): each counts as a failed deploy, so the release starts again. Returns their deploy ids."""
    out = []
    for r in conn.execute("SELECT * FROM hook_runs WHERE event='deploy' AND started_at IS NOT NULL AND ended_at IS NULL "
                          "ORDER BY id").fetchall():
        limit = parse_duration(setting(conn, "deploy_timeout", item_id=r["deploy_id"])).total_seconds()
        if (now() - parse_iso(r["started_at"])).total_seconds() < limit + DEPLOY_HOOK_LOST:
            continue
        with tx(conn):
            if conn.execute("UPDATE hook_runs SET ended_at=? WHERE id=? AND ended_at IS NULL",
                            (iso(now()), r["id"])).rowcount == 0:
                continue
        _deploy_hook_end(conn, dict(r), False, "the run did not end (maxpm serve stopped while it ran)", "", None)
        out.append(r["deploy_id"])
    return out


def _deploy_hook_end(conn, run, ok, result, tail, folder):
    """The deploy hook ended. Exit 0: the deploy item is done (the deployed hook runs).
    Else the deploy item stays open, and the release starts again from its beginning: it is not cut, a fix item
    that it waits on holds the failure, a new review covers the fix (with review on), and when that review passes
    maxpm serve runs the hook again. Nobody runs the command again by hand or repairs production by hand."""
    dep_id, target, cmd = run["deploy_id"], run["target"], run["command"]
    with tx(conn):
        dep = _item(conn, dep_id)
        conn.execute("UPDATE items SET blocked_reason=NULL, blocked_at=NULL, blocked_until=NULL, blocked_set_by=NULL "
                     "WHERE id=? AND blocked_set_by=?", (dep_id, DEPLOY_HOOK_BY))
        if dep["status"] != "open":
            return {"deploy_end": f"#{dep_id} is {dep['status']}: nothing to do"}
    last = (tail or "").strip().splitlines()[-1:] if tail else []
    if ok:
        done(conn, dep_id, f"deploy hook: {result}: {cmd}" + (f" ({last[0][:200]})" if last else ""), "maxpm")
        run_hooks(conn, folder)  # the deployed hook of this close
        return {"deploy_end": "done"}
    with tx(conn):
        dep = _item(conn, dep_id)
        tg = _target(conn, target)
        ships = conn.execute("SELECT i.project_id FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=? "
                             "AND i.kind NOT IN ('review','deploy') ORDER BY i.id LIMIT 1", (dep_id,)).fetchone()
        project_id = ships["project_id"] if ships else dep["project_id"]
        conn.execute("UPDATE items SET cut_at=NULL, cut_rev=NULL WHERE id=?", (dep_id,))
        top = conn.execute("SELECT COALESCE(MAX(rank),0) m FROM items WHERE project_id=?", (project_id,)).fetchone()["m"]
        context = (f"The deploy hook of target {target} failed ({result}) for deploy #{dep_id}: {cmd}"
                   + (f" (in {folder})" if folder else "") + (f"\nThe last lines of its output:\n{tail[-2000:]}" if tail else "")
                   + "\nFind the cause and fix it in the code or in the release script, and commit it. Do not run the "
                   "release command again and do not repair production by hand: when this item is done, the release "
                   "starts again from its beginning (a new review, then maxpm serve runs the deploy hook again).")
        cur = conn.execute(
            "INSERT INTO items(project_id,title,priority,rank,doer,context,kind,target,found_during,created_at) "
            "VALUES (?,?,?,?,'ai',?,'work',?,?,?)",
            (project_id, f"Fix the failed deploy of {target} (deploy #{dep_id}): {result}", dep["priority"], top + 1,
             context, target, dep_id, iso(now())))
        fix = cur.lastrowid
        _event(conn, fix, "maxpm", f"added: the deploy hook of #{dep_id} failed ({result})")
        _dep_add(conn, dep_id, fix, "maxpm", alert=False)
        for a in _approvals(conn, dep_id):  # the release changes: it takes a new yes (release_approval person)
            conn.execute("DELETE FROM deps WHERE item_id=? AND blocked_by=?", (dep_id, a["id"]))
        merged = _merge_later_deploys(conn, dep_id, target)
        rv = _release_review(conn, dep, tg, "maxpm")
        _event(conn, dep_id, "maxpm", f"deploy hook failed ({result}); the release starts again: it is not cut, it "
                                      f"waits on fix #{fix}" + (f" and a new review #{rv['id']}" if rv else "")
                                      + (f"; it takes in the later deploy item(s) {', '.join(f'#{m}' for m in merged)}"
                                         if merged else ""))
        to = tg["owner"] or active_manager(conn)
        if to:
            _send(conn, "alert", "maxpm", f"the deploy hook of target {target} failed ({result}) for deploy #{dep_id}: "
                  f"{cmd}" + (f"\n{tail[-1500:]}" if tail else "") + f"\nThe deploy item stays open, and the release "
                  f"starts again: fix #{fix}" + (f", then a new review #{rv['id']}" if rv else "")
                  + ", then maxpm serve runs the hook again. Do not run the command by hand.", to=to, item_id=dep_id)
    return {"deploy_end": "failed", "fix": fix, "review": rv["id"] if rv else None, "alerted": to}


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


def target_cadence(conn, name, value, actor=None, reason=None):
    """How often the target releases (2h, 1d, 1w, 1mo; off: each release starts at once). It applies from now
    on, also to the release that collects ship requests now, unless that one has started. reason: why it fits the
    project's needs (#5003); a new cadence without one keeps no old reason."""
    text, _ = parse_cadence(value)
    reason = " ".join((reason or "").split())
    with tx(conn):
        name = _target(conn, name)["name"]
        conn.execute("UPDATE targets SET cadence=?, cadence_reason=? WHERE name=?", (text, reason, name))
        _event(conn, None, actor, f"target {name} release cadence {text or 'off'}" + (f" ({reason})" if reason else ""))
        _cadence_sync(conn)
    return target_show(conn, name)


def _span(td):
    """A time left for a person: 45m, 3h20m, and 2d04h from two days on."""
    h = max(0, int(td.total_seconds() // 3600))
    return f"{h // 24}d{h % 24:02d}h" if h >= 48 else _short(td)


def _merge_later_deploys(conn, dep_id, target):
    """The deploy hook of a cut release failed, and the release starts again uncut (_deploy_hook_end). A later deploy
    item of the target collected the ship requests meanwhile: two open releases of one target then waited on each
    other (#5074, 2026-11-10: #4912 failed, its new review waited on the release order of #5017, cut by its own
    reviewer a few seconds later, and on the cadence from that cut). One release of a target is open: the failed
    one takes in each later open deploy item that no session deploys now. Their ship requests move to it, their
    open reviews are dropped (the new review covers their items; a reviewer that held one gets a notice), and they
    are dropped as merged. Inside a tx. Returns the ids merged."""
    merged = []
    for o in conn.execute("SELECT * FROM items WHERE kind='deploy' AND target=? AND status='open' AND id<>? ORDER BY id",
                          (target, dep_id)).fetchall():
        for r in conn.execute("SELECT i.* FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=?",
                              (o["id"],)).fetchall():
            if r["kind"] == "review":
                if r["status"] in OPEN_STATES:
                    conn.execute("UPDATE items SET status='dropped', closed_at=?, assignee=NULL, lease_expires_at=NULL, "
                                 "reserved_for=NULL, reserved_until=NULL WHERE id=?", (iso(now()), r["id"]))
                    _event(conn, r["id"], "maxpm", f"dropped: its release #{o['id']} joins release #{dep_id}, whose "
                                                   f"deploy failed; the new review of #{dep_id} covers its items")
                    if r["assignee"]:
                        _send(conn, "notice", "maxpm", f"review #{r['id']} is dropped: its release #{o['id']} joins "
                              f"release #{dep_id} of {target}, whose deploy hook failed. Stop the review; the new "
                              f"review of #{dep_id} covers its items.", to=r["assignee"], item_id=r["id"])
            elif r["kind"] != "deploy" and not conn.execute("SELECT 1 FROM deps WHERE item_id=? AND blocked_by=?",
                                                            (dep_id, r["id"])).fetchone():
                _dep_add(conn, dep_id, r["id"], "maxpm", alert=False)
        conn.execute("UPDATE items SET status='dropped', closed_at=?, cut_at=NULL, blocked_reason=NULL, blocked_at=NULL, "
                     "blocked_until=NULL, blocked_set_by=NULL WHERE id=?", (iso(now()), o["id"]))
        _event(conn, o["id"], "maxpm", f"dropped: merged into release #{dep_id} of {target}, whose deploy hook failed "
                                       f"(one open release per target)")
        merged.append(o["id"])
    return merged


def _deploy_failed(conn, dep_id):
    """Whether the deploy hook of this deploy item failed before: the release runs again after a fix."""
    return conn.execute("SELECT 1 FROM events WHERE item_id=? AND change LIKE 'deploy hook failed%'",
                        (dep_id,)).fetchone() is not None


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
    out = {"target": name, "cadence": text, "cadence_reason": t["cadence_reason"],
           "approval": release_approval(conn, name), "deploy": dep["id"] if dep else None, "start": None, "early": None,
           "ships": [], "waits": False, "running": cur["id"] if cur else None,
           "current": {"id": cur["id"], "status": cur["status"], "cut_at": cur["cut_at"] or cur["claimed_at"],
                       "text": show_time(cur["cut_at"] or cur["claimed_at"], zone)} if cur else None,
           "last": {"id": last["id"], "at": last["at"], "text": show_time(last["at"], zone),
                    "cut": bool(last["cut_at"])} if last else None,
           "next_at": iso(parse_iso(last["at"]) + cad) if cad and last else None}
    later = out["next_at"] is not None and out["next_at"] > iso(now())  # the cadence does not permit it yet
    if later and dep and _deploy_failed(conn, dep["id"]):
        later = False  # it was released in its time already: its run after a fix waits for no cadence (#5074)
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
        rev = (rev or "").strip() or None
        cut = _item(conn, plan["running"]) if plan["running"] and (plan["deploy"] is None or not plan["ships"]) else None
        if rev and cut and cut["status"] == "open" and cut["cut_at"]:
            # The release is cut and does not deploy yet: a new revision moves its pin (#5074; the pin of a failed
            # deploy stayed, and the manager rewrote the pin file by hand). The release-cut hook runs again with it.
            conn.execute("UPDATE items SET cut_rev=? WHERE id=?", (rev, cut["id"]))
            _event(conn, cut["id"], actor, f"release revision moved to {rev} by {actor or 'a person'} "
                                           f"(was {cut['cut_rev'] or 'none'})")
            _add_note(conn, cut["id"], f"[maxpm] release revision moved at {iso(now())}: {rev}")
            _hook(conn, "release-cut", name, cut["id"], actor)
            return dict(target_show(conn, name), cut=item_show(conn, cut["id"]), items=[], moved=rev)
        if plan["deploy"] is None or not plan["ships"]:
            raise RiverError(f"nothing is collected for {name}: ask for items to go out first "
                             f"(maxpm ship <id>, or done --ship)"
                             + (f". Release #{plan['running']} is cut already (maxpm show {plan['running']}); to move "
                                f"the revision it builds from: maxpm target cut {name} --rev <commit>"
                                if plan["running"] else ""))
        if plan["waits"]:
            raise RiverError(f"{plan['text']}. To cut it sooner, give the reason first: "
                             f"maxpm target release-now {name} --reason \"<why>\"")
        _release_cut(conn, plan["deploy"], actor, f"maxpm target cut by {actor or 'a person'}", rev)
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
    """How many agent sessions are live now (not gone, not stopped), for max_sessions. An assistant (#5110) takes no
    queue work, so it uses no slot."""
    return sum(1 for r in conn.execute("SELECT * FROM agents WHERE kind='ai' AND COALESCE(role, '')<>'assistant'")
               if _agent_state(conn, r) not in ("gone", "stopped"))


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
                      "ready": a["ready"], "ships": ships(a), "review": review(a), "cut_at": a["cut_at"]}
                     for a in pending],
            release=release_plan(conn, t["name"]), hooks=target_hooks(conn, t["name"]),
            last_deploy=({"id": last["id"], "title": last["title"], "closed_at": last["closed_at"],
                          "output": last["output"], "ships": ships(last)} if last else None),
            history=[{"id": a["id"], "title": a["title"], "closed_at": a["closed_at"], "output": a["output"],
                      "ships": ships(a), "done_by": _done_by(conn, a["id"])}
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


TIERS = ("normal", "expert")
# The kinds of a task that maxpm add and maxpm edit set (#4973). operational: work on the operation of the system
# itself (mark: 'cleaning tasks, changing graph orderings, reading prod logs to understand what a task needs to be
# created with the right context'). It has its own default model (KIND_MODELS), and it starts in a new session.
TASK_KINDS = ("work", "operational")


def _check_kind(kind):
    if kind is None:
        return None
    k = str(kind).strip().lower()
    if k not in TASK_KINDS:
        raise RiverError(f"the kind of a task is {' or '.join(TASK_KINDS)}")
    return k


def _check_tier(tier):
    """The tier of a task (#4959): 'expert' (an expert session takes it; the person talks it through with it), or
    None for a normal task ('normal' or '' clears it)."""
    if tier is None:
        return None
    t = str(tier).strip().lower()
    if t not in TIERS + ("",):
        raise RiverError(f"tier is {' or '.join(TIERS)}")
    return "expert" if t == "expert" else ""


SHORT_WORDS = 8     # a short title or a headline: about 6 words, at most this many (#5086)
SHORT_CHARS = 50
DOING_WORDS = 14    # what a session does now: about 10 words
DOING_CHARS = 90
SHORT_RULE = ("about 6 plain words that name the work, as a person says it: no item ids, no file names, no quotes, "
              "no commands")


DOING_RULE = "plain words for a person: no item ids, no file names, no links, no commands"


def _check_words(text, words, chars, what, example, rule=SHORT_RULE):
    t = " ".join(str(text or "").split())
    if len(t.split()) > words or len(t) > chars:
        raise RiverError(f"{what} is too long ({len(t.split())} words, {len(t)} characters): at most {words} words "
                         f"and {chars} characters, {rule}. For example: {example}")
    if re.search(r"(?<![\w&])#\d+|\bhttps?://|\S/|/\S|`", t):
        raise RiverError(f"{what} {t!r} has an id, a link, a path or code: {rule}. For example: {example}")
    return t


def check_short(text):
    """An item's short title, cleaned; "" clears it. The pages show it; the title stays the full text (#5086)."""
    return _check_words(text, SHORT_WORDS, SHORT_CHARS, "the short title", "'Fix the Windows test failures'")


def short_title(title, short=None, words=SHORT_WORDS - 1):
    """The short title of an item, else the first words of its title: no leading '#<id>' and no '(...)', cut at the
    first ':', ';' or dash that leaves three words or more, and at most `words` words (#5086). The page has the same rule
    (lib.js shortTitle)."""
    if short:
        return short
    t = re.sub(r"\s*\([^()]*\)", "", re.sub(r"^#\d+\s+", "", str(title or "")))
    t = " ".join(t.split())
    m = re.search(r"[:;]|\s[-\u2013\u2014]\s", t)
    if m and len(t[:m.start()].split()) >= 3:
        t = t[:m.start()].rstrip(" ,.")
    w = t.split()
    if len(w) > words:
        t = " ".join(w[:words]).rstrip(" ,.:;") + "\u2026"
    return t if len(t) <= 60 else t[:59].rstrip() + "\u2026"


def agent_now(conn, name, doing, headline=None):
    """maxpm now: what a session does now (about 10 words) and, with headline, a short name of its work (about 6),
    in its own plain words. The Sessions tab shows them as the two lines of its row (#5086)."""
    doing = _check_words(doing, DOING_WORDS, DOING_CHARS, "the text", "'Runs the tests after the fix'",
                         DOING_RULE) if doing else ""
    head = _check_words(headline, SHORT_WORDS, SHORT_CHARS, "the headline", "'Short titles on every page'") \
        if headline is not None else None
    if not doing and not head:
        raise RiverError('say what you do now: maxpm now "<about 10 words>" [--work "<about 6 words>"]')
    at = iso(now())
    with tx(conn):
        _agent(conn, name)
        if doing:
            conn.execute("UPDATE agents SET doing=?, doing_at=? WHERE name=?", (doing, at, name))
        if head is not None:
            conn.execute("UPDATE agents SET headline=?, headline_at=? WHERE name=?", (head or None, at, name))
    return {"name": name, "doing": doing or None, "headline": head}


def item_add(conn, project, title, priority=2, notes="", doer="any", after=(), actor=None,
             context="", touches=None, check="", blocks=None, mode=None, found_during=None, feeds=(), due=None,
             goals=None, refs=None, models=None, backlog=None, check_similar=False, new=None, mine=False, tier=None,
             kind=None, short=None):
    """Add an item. `short`: the short title the pages show (check_short); the title stays the full one. `after`: items it waits on. `feeds`: items it waits on and whose output it reads.
    `backlog`: a note on why nobody takes it now; the item goes to the backlog at once.
    `check_similar` (maxpm add): refuse with SimilarWork when the same work is open or was done lately
    (similar_items); `new` says how this item differs, and adds it anyway (the history keeps it).
    `mine` (maxpm add --mine): the actor adds the item for work it does now, so it is reserved for the
    actor at once, with no time limit, in the same tx. Then no other session gets it from go, next, or wait,
    and Dispatch and the launches refuse it; it is free again when the actor's session stops or is gone
    (_sweep). Without it, a session that adds an item for its own work while it holds another item saw a
    second session start the same work within seconds (#4942)."""
    tier = _check_tier(tier)
    kind = _check_kind(kind)
    short = check_short(short) if short is not None else None
    if kind == "operational" and doer == "human":
        raise RiverError("an operational task is for an agent session; leave out --kind, or --doer human")
    if tier and doer == "human":
        raise RiverError("an expert-tier task is for an agent session that a person talks with; a person's item has "
                         "no tier: leave out --tier, or --doer human")
    if mine:
        if not actor:
            raise RiverError("--mine needs an agent name: set MAXPM_AGENT or pass --as <name>")
        if doer == "human":
            raise RiverError("--mine reserves the item for you, and a person's item is for the person: "
                             "leave out --mine, or --doer human")
        if backlog is not None:
            raise RiverError("--mine is for work you do now, and the backlog is for work nobody does now: "
                             "leave out one of them")
    if backlog is not None and blocks is not None:
        raise RiverError(f"refused: #{blocks} would wait on a backlog item. Add the item without --blocks, or "
                         f"without --backlog")
    if doer not in DOERS:
        raise RiverError(f"doer is one of {', '.join(DOERS)}")
    if not (0 <= int(priority) <= 4):
        raise RiverError("priority is 0 (highest) to 4 (lowest)")
    if new is not None and not new.strip():
        raise RiverError("say how this item differs from the matches: --new \"<how it differs>\"")
    like = similar_items(conn, project, title, context, touches, [blocks, found_during, *after, *(feeds or ())],
                         actor, doer=doer) if check_similar or new else []
    if like and check_similar and new is None:
        raise SimilarWork(like)
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
        if short:
            conn.execute("UPDATE items SET short=? WHERE id=?", (short, iid))
        if new is not None:
            _event(conn, iid, actor, f"added as new work" + (f" although like {', '.join('#' + str(m['id']) for m in like)}"
                                                             if like else "") + f": {new.strip()}")
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
        if backlog is not None:
            why = backlog.strip() or "added to the backlog"
            conn.execute("UPDATE items SET backlog_at=?, backlog_note=? WHERE id=?", (iso(now()), why, iid))
            _event(conn, iid, actor, f"moved to the backlog: {why}")
        # Only an expert task inherits the model of the work it takes up (#4979, mark: 'we do not use fable except
        # when specifically asked and for expert discussions'); a plain item, also one found during or added before
        # a Fable item, takes the default model and limits.
        src = found_during if found_during is not None else blocks
        if src is not None and tier == "expert" and not (models and any(models.get(f) for f in
                                                                        ("model", "min_model", "max_model"))):
            _inherit_model(conn, iid, int(src), actor)
        if kind and kind != "work":
            conn.execute("UPDATE items SET kind=? WHERE id=?", (kind, iid))
            _event(conn, iid, actor, f"kind {kind}: work on the operation of the system; it starts in a new session")
        if tier:
            conn.execute("UPDATE items SET tier=? WHERE id=?", (tier, iid))
            _event(conn, iid, actor, f"tier {tier}: an expert session takes it, and a person talks it through with it")
        if mine:
            _agent(conn, actor)
            conn.execute("UPDATE items SET reserved_for=?, reserved_until=NULL, reserved_by=? WHERE id=?",
                         (actor, actor, iid))
            _event(conn, iid, actor, f"reserved for {actor}: added for its own work (maxpm add --mine)")
    a = item_show(conn, iid)
    if mine:
        a["mine"] = True
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
        fam = platform_spec(new["agent"])["family"]
        for f in ("model", "min_model", "max_model") if fam in ladder else ():  # "multi": any provider/model text
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
              models=None, tier=None, kind=None, short=None):
    tier = _check_tier(tier)
    kind = _check_kind(kind)
    short = check_short(short) if short is not None else None
    with tx(conn):
        it = _item(conn, item_id)
        if short is not None and (it["short"] or "") != short:
            conn.execute("UPDATE items SET short=? WHERE id=?", (short or None, it["id"]))
            _event(conn, it["id"], actor, f"short title: {short}" if short else "short title removed")
        if kind is not None and it["kind"] != kind:
            if it["kind"] not in TASK_KINDS:
                raise RiverError(f"#{it['id']} is a {it['kind']} item; only a task of kind {' or '.join(TASK_KINDS)} "
                                 f"changes its kind")
            if kind == "operational" and it["doer"] == "human":
                raise RiverError(f"#{it['id']} is a person's item; an operational task is for an agent session")
            conn.execute("UPDATE items SET kind=? WHERE id=?", (kind, it["id"]))
            _event(conn, it["id"], actor, f"kind {kind}")
        if tier is not None and (it["tier"] or "") != tier:
            if tier and it["doer"] == "human":
                raise RiverError(f"#{it['id']} is a person's item; an expert-tier task is for an agent session")
            conn.execute("UPDATE items SET tier=? WHERE id=?", (tier or None, it["id"]))
            _event(conn, it["id"], actor, f"tier {tier or 'normal'}")
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
    someone splits it or changes its scope (maxpm replanned clears it)."""
    conn.execute("UPDATE items SET late_prereqs=late_prereqs+? WHERE id=?", (n, parent))
    it = _item(conn, parent)
    limit = int(setting(conn, "replan_threshold", item_id=parent, agent=actor))
    if it["late_prereqs"] >= limit and not it["replan"]:
        conn.execute("UPDATE items SET replan=1 WHERE id=?", (parent,))
        _event(conn, parent, "maxpm", f"marked replan: {it['late_prereqs']} prerequisites added while it was "
               f"claimed (replan_threshold {limit})")


def replanned(conn, item_id, note=None, actor=None):
    """Clear the replan mark after someone looked at the item again; the count starts over."""
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
        _refuse_backlog(it, "pushed")
        if it["reserved_for"] and it["reserved_for"] not in (to, actor):  # its own reservation, the actor hands on
            raise RiverError(f"#{item_id} is already reserved for {it['reserved_for']}"
                             + (" (pushed)" if it["reserved_until"] else "") + f"; {_unreserve_hint(item_id)}")
        q = conn.execute("SELECT agent FROM queue_entries WHERE item_id=?", (it["id"],)).fetchone()
        if q and q["agent"] != to:  # a push would hold it against the queue, and nobody could claim it
            raise RiverError(f"#{item_id} is in the queue of {q['agent']}; remove it there first: "
                             f"maxpm queue remove {q['agent']} {item_id}")
        why = reviewer_refusal(conn, it, to)
        if why:
            raise RiverError(f"refused: {to}: {why}. Push it to a session that held no task and did not work on the "
                             f"release, or let maxpm serve start one")
        _refuse_model_to(conn, it, to, "push")
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
        _refuse_model_to(conn, it, to, "give")
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
        if it["blocked_set_by"] == DEPLOY_HOOK_BY:
            raise RiverError(f"#{it['id']} waits while the deploy hook of target {it['target']} runs (maxpm serve); "
                             f"the hook ends the wait")
        if it["blocked_set_by"] == CADENCE_BY:
            raise RiverError(f"#{it['id']} waits for the release cadence or the release order of target "
                             f"{it['target']}; a release sooner needs a reason: "
                             f"maxpm target release-now {it['target']} --reason \"<why>\"")
        conn.execute("UPDATE items SET blocked_reason=NULL, blocked_until=NULL, blocked_at=NULL, blocked_set_by=NULL "
                     "WHERE id=?", (it["id"],))
        _event(conn, it["id"], actor, "outside blocker cleared")
    return item_show(conn, item_id)


# ---------------------------------------------------------------- backlog

def _refuse_backlog(it, verb):
    if it["backlog_at"] and it["status"] == "open":
        raise RiverError(f"refused: #{it['id']} is in the backlog, so it is not {verb}; a person or the manager makes "
                         f"it open again first: maxpm activate {it['id']}")


def _waiting_on(conn, item_id):
    """Open items, not in the backlog themselves, that wait on this one (blocks or feeds links)."""
    return [dict(r) for r in conn.execute(
        f"SELECT i.id, i.title FROM deps d JOIN items i ON i.id=d.item_id WHERE d.blocked_by=? AND d.kind<>'conflicts' "
        f"AND i.status IN {OPEN_STATES} AND i.backlog_at IS NULL ORDER BY i.id", (item_id,))]


def backlog(conn, item_id, note=None, actor=None, force=None):
    """Move an open item to the backlog: it keeps its notes, links, and priority, but go, next, launch, and
    the notifications never offer it. Items that wait on it keep waiting, so a link needs `force`."""
    if not (note or "").strip():
        raise RiverError(f"say why nobody takes it now: maxpm backlog {item_id} --note \"<why not now>\"")
    with tx(conn):
        it = _item(conn, item_id)
        if it["backlog_at"]:
            raise RiverError(f"#{item_id} is in the backlog already (since {it['backlog_at'][:16]}); "
                             f"maxpm activate {item_id} makes it open again")
        if it["status"] in CLOSED_STATES:
            raise RiverError(f"#{item_id} is {it['status']}; only an open item goes to the backlog "
                             f"(maxpm reopen {item_id} first)")
        if it["status"] != "open":
            raise RiverError(f"refused: {it['assignee'] or 'an agent'} holds #{item_id} ({it['status'].replace('_', ' ')}). "
                             f"Release it first (maxpm release {item_id} --note \"<why>\"), then move it to the backlog")
        if it["kind"] in ("deploy", "review", "monitor"):
            raise RiverError(f"refused: #{item_id} is a {it['kind']} item, which MaximizePM makes for a release; "
                             f"only work items go to the backlog")
        waiting = _waiting_on(conn, item_id)
        if waiting and not (force or "").strip():
            raise RiverError(f"#{item_id} is a prerequisite of open items: "
                             + ", ".join(f"#{w['id']} {w['title']}" for w in waiting)
                             + f". They cannot be ready while #{item_id} is in the backlog. Remove the links "
                             f"(maxpm undep <id> --on {item_id}), move them to the backlog first, or say why they may "
                             f"wait: maxpm backlog {item_id} --note \"...\" --force \"<why>\"")
        t = iso(now())
        conn.execute("UPDATE items SET backlog_at=?, backlog_note=?, reserved_for=NULL, reserved_until=NULL, "
                     "reserved_by=NULL, fresh_start=NULL WHERE id=?", (t, note.strip(), item_id))
        if conn.execute("DELETE FROM queue_entries WHERE item_id=?", (item_id,)).rowcount:
            _event(conn, item_id, actor, "left the queue (moved to the backlog)")
        _event(conn, item_id, actor, f"moved to the backlog: {note.strip()}"
               + (f" (items wait on it: {', '.join('#' + str(w['id']) for w in waiting)}; {force.strip()})"
                  if waiting else ""))
    return {"id": item_id, "title": it["title"], "backlog_at": t, "note": note.strip(),
            "waiting": [w["id"] for w in waiting]}


def activate(conn, item_id, actor=None, note=None):
    """Take an item out of the backlog: it is open again, and ready when nothing it waits on is open."""
    with tx(conn):
        it = _item(conn, item_id)
        if not it["backlog_at"]:
            raise RiverError(f"#{item_id} is not in the backlog (status {it['status'].replace('_', ' ')})")
        conn.execute("UPDATE items SET backlog_at=NULL, backlog_note=NULL WHERE id=?", (item_id,))
        _event(conn, item_id, actor, "taken from the backlog" + (f": {note.strip()}" if (note or "").strip() else ""))
    a = annotate(conn)[item_id]
    return {"id": item_id, "title": a["title"], "ready": a["ready"], "status": a["status"],
            "open_blockers": a["open_blockers"]}


def backlog_list(conn, project=None, ann=None):
    """Backlog items by project rank, then priority and the queue order, with the open items that wait on each."""
    ann = ann or annotate(conn)
    names = _names(conn, project) if project else None
    rows = sorted((a for a in ann.values() if a["backlog"] and not a["project_archived"]
                   and (names is None or a["project"] in names)),
                  key=lambda a: (a["project_rank"], a["project"], a["priority"], a["rank"], a["id"]))
    return [{"id": a["id"], "title": a["title"], "project": a["project"], "priority": a["priority"],
             "doer": a["doer"], "backlog_at": a["backlog_at"], "backlog_note": a["backlog_note"], "goals": a["goals"],
             "waited_by": sorted(d for d in a["unblocks"] if ann[d]["status"] in OPEN_STATES and not ann[d]["backlog"])}
            for a in rows]


# ---------------------------------------------------------------- search and similar items (#2257)
#
# One score for maxpm search, the check of maxpm add, the related items of show and go, and maxpm duplicates:
# the cosine of two word vectors, each word weighted by how rare it is in the queue (idf). A title word counts
# twice, a context word once. Measured on a live queue of 2,012 work items (2026-10-14): at 0.55, the check
# stops 2.4% of adds and catches every known repeat (scores 0.59 to 0.80); at 0.45 it stops 6.9%.

_STOP_WORDS = frozenset("""a an the and or but of to in on for with by from at as is are be was were it its this that
these those not no do does done into than then when if so via per one two each every all any new add adds added
make makes item items maxpm after before only also same can cannot has have""".split())
SIMILAR_BATCH = timedelta(hours=2)  # items one author added this close together are a series, not repeats
RELATED_MIN = 0.3                   # the score from which show and go list an item as related


def _stem(w):
    for s in ("ing", "ed", "es", "s"):
        if len(w) > len(s) + 3 and w.endswith(s):
            return w[:-len(s)]
    return w


def _words(text):
    """The words of a text that tell items apart: lower case, no common words, a light stem (tests -> test)."""
    return [_stem(w) for w in re.findall(r"[a-z0-9_]+", (text or "").lower())
            if w not in _STOP_WORDS and (len(w) > 2 or w.isdigit())]


def _text_rows(conn, where="1", params=()):
    """Items with their words: `key` (title twice, context once) for the score, `all` (title, context, notes,
    output) for a search."""
    rows = []
    for r in conn.execute("SELECT i.id, i.project_id, p.name project, p.path, i.title, i.context, i.notes, i.output, "
                          "i.status, i.kind, i.doer, i.assignee, i.touches, i.created_at, i.closed_at, i.backlog_at, "
                          "i.found_during FROM items i JOIN projects p ON p.id=i.project_id WHERE " + where, params):
        d = dict(r)
        title, context = _words(d["title"]), _words(d["context"])
        d["key"] = title + title + sorted(set(context))
        d["all"] = set(title) | set(context) | set(_words(d["notes"])) | set(_words(d["output"]))
        rows.append(d)
    return rows


def _idf(conn):
    """How rare each word is among the work items' titles and contexts."""
    df, n = {}, 0
    for r in conn.execute("SELECT title, context FROM items WHERE kind='work'"):
        n += 1
        for w in set(_words(r["title"]) + _words(r["context"])):
            df[w] = df.get(w, 0) + 1
    return lambda w: math.log((n + 1) / (df.get(w, 0) + 1)) + 1


def _vector(words, idf):
    v = {}
    for w in words:
        v[w] = v.get(w, 0) + idf(w)
    return v


def _cosine(a, b):
    dot = sum(x * b[w] for w, x in a.items() if w in b)
    if not dot:
        return 0.0
    return dot / (math.sqrt(sum(x * x for x in a.values())) * math.sqrt(sum(x * x for x in b.values())))


# Identifiers (#3286): the names of one thing, which two texts share even when their words differ. An item
# number, a URL, a mail address, a path, a hex id (a uuid or its prefix, a commit), or a quoted name ('Save').
# Measured on a live queue (2026-10-23, 128 open items in one folder): two person items that ask for a look at
# the same step scored 0.50, and share its quoted name; a reset item and the person's item it made unnecessary
# scored 0.32, and share a path. An identifier that more than IDENT_COMMON open items name is a common place
# (a file every item touches, the release they all wait on): it tells nothing.
_UUID_RE = re.compile(r"(?<![0-9a-zA-Z])([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-zA-Z])")
_URL_RE = re.compile(r"https?://[^\s'\"<>()\[\]]+")
_MAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PATH_RE = re.compile(r"(?<![\w/~.-])(?:~/|/)?[\w.-]+(?:/[\w.-]+)+")
_HEX_RE = re.compile(r"(?<![0-9a-zA-Z])[0-9a-f]{7,}(?![0-9a-zA-Z])")
_REF_RE = re.compile(r"(?<![\w&])#(\d+)\b")
_QUOTE_RE = re.compile(r"(?<!\w)['\"‘“]([^'\"‘’“”\n]{3,80}?)['\"’”](?!\w)")
IDENT_COMMON = 4
IDENT_BONUS = 0.2   # what a shared identifier adds to the score of two person items, and to a close check
CLOSE_NEAR = 0.4    # the score from which a close lists an open item that names one of its identifiers
CLOSE_LIMIT = 6     # at most this many near items in one close check (linked items always)


def _idents(text):
    """The identifiers in a text, in one form each: lower case, a hex id cut to 7 characters (a uuid prefix and
    the whole uuid match; a whole uuid also adds uuid:<id>, which a commit id never has), a quoted name of two
    words or more in quotes ('before you connect google')."""
    text = text or ""
    out = {m[:7] for m in _UUID_RE.findall(text)} | {f"uuid:{m[:7]}" for m in _UUID_RE.findall(text)}
    out |= {m.rstrip(".,;:").lower() for m in _URL_RE.findall(text)}
    out |= {m.rstrip(".").lower() for m in _MAIL_RE.findall(text)}
    rest = _MAIL_RE.sub(" ", _URL_RE.sub(" ", _UUID_RE.sub(" ", text)))
    for m in _PATH_RE.findall(rest):
        m = m.rstrip(".")
        if len(m) >= 6 and ("." in m.rsplit("/", 1)[-1] or m[0] in "/~"):
            out.add(m.lower())
    out |= {m[:7] for m in _HEX_RE.findall(rest) if re.search(r"\d", m) and re.search(r"[a-f]", m)}
    out |= {"#" + m for m in _REF_RE.findall(text)}
    for m in _QUOTE_RE.findall(text):
        q = " ".join(m.split()).strip(" .,;:").lower()
        if len(q) >= 8 and " " in q:
            out.add(f"'{q}'")
    return out


def _item_idents(d):
    return _idents(" ".join(x or "" for x in (d["title"], d["context"], d["notes"])))


def _ident_counts(rows):
    """How many of the rows (open items) name each identifier."""
    n = {}
    for d in rows:
        for x in d.setdefault("idents", _item_idents(d)):
            n[x] = n.get(x, 0) + 1
    return n


def _places(idents):
    """The identifiers that name a place or a thing, not an item or a person: what two person asks share when
    they ask for the same step (#id and mail addresses tie unrelated asks together)."""
    return {x for x in idents if not x.startswith("#") and "@" not in x}


def _state(d):
    return "backlog" if d["backlog_at"] and d["status"] == "open" else d["status"]


def _match(d, score):
    return {"id": d["id"], "title": d["title"], "project": d["project"], "status": _state(d), "doer": d["doer"],
            "assignee": d["assignee"], "output": d["output"], "created_at": d["created_at"],
            "closed_at": d["closed_at"], "score": round(score, 2)}


def _since(conn, text):
    """--since 30d (a time back from now) or a date (2026-10-01)."""
    try:
        return iso(now() - parse_duration(text))
    except RiverError:
        return iso(parse_when(text, setting(conn, "timezone")))


def search(conn, text, project=None, status=None, since=None, limit=20, kinds=False):
    """Items whose title, context, notes, or output hold every word of the text, newest first: open, done,
    and dropped. Only work items, unless kinds (deploy, review, monitor and fixes items too). No item with every
    word: the closest items by the score, so a search never comes back empty-handed when something is near."""
    want = _words(text)
    if not want:
        raise RiverError("give words to search for: maxpm search \"<words>\"")
    states = {"open": ("open",), "in_progress": ("in_progress",), "held": ("held",), "done": ("done",),
              "dropped": ("dropped",), "closed": CLOSED_STATES, "backlog": ("open",), "all": None}
    if status and status not in states:
        raise RiverError(f"--status is one of {', '.join(states)}")
    where, params = ["1"], []
    if not kinds:
        where.append("i.kind='work'")
    if project:
        names = _names(conn, project)
        where.append(f"p.name IN ({','.join('?' * len(names))})")
        params += names
    if status and states[status]:
        where.append(f"i.status IN ({','.join('?' * len(states[status]))})")
        params += states[status]
        if status in ("open", "backlog"):
            where.append("i.backlog_at IS " + ("NOT NULL" if status == "backlog" else "NULL"))
    if since:
        t = _since(conn, since)
        where.append("(i.created_at>=? OR i.closed_at>=?)")
        params += [t, t]
    rows = _text_rows(conn, " AND ".join(where), params)
    hits = sorted((d for d in rows if all(w in d["all"] for w in want)), key=lambda d: (d["created_at"], d["id"]),
                  reverse=True)
    res = {"query": text, "words": want, "total": len(hits), "items": [_match(d, 1) for d in hits[:limit]],
           "closest": []}
    for m in res["items"]:
        del m["score"]
    if not hits:
        idf = _idf(conn)
        q = _vector(want, idf)
        near = sorted(((_cosine(q, _vector(d["key"], idf)), d) for d in rows), key=lambda x: -x[0])
        res["closest"] = [_match(d, s) for s, d in near[:min(limit, 5)] if s >= 0.2]
    return res


def _recent_batch(conn, actor):
    """Items this actor added in the last SIMILAR_BATCH: a planner's series, which the add check passes over."""
    if not actor:
        return set()
    return {r["item_id"] for r in conn.execute(
        "SELECT item_id FROM events WHERE actor=? AND change LIKE 'added to %' AND at>=?",
        (actor, iso(now() - SIMILAR_BATCH)))}


def similar_items(conn, project, title, context="", touches=None, linked=(), actor=None, limit=5, doer=None):
    """What maxpm add checks before it adds: open items of the project, and items done in the last similar_days,
    whose score against the new title and context reaches similar_min. An open item whose touches overlap the new
    ones scores 0.15 more; for a person's item, a person's item that names the same place (a quoted name, URL,
    path, or id; _places) scores IDENT_BONUS more. Items the new one names (--blocks, --after, --feeds,
    --found-during) and the series that the same author added in the last two hours are left out: the author
    knows them. The series stays out for person items too: on the live queue a planner's series of person asks
    (one per vendor or per person) scored 0.55 to 0.87 against each other, with no repeat among them."""
    p = _project(conn, project)
    floor = setting(conn, "similar_min", project_id=p["id"])
    if floor == "off":
        return []
    floor = float(floor)
    since = iso(now() - parse_duration(setting(conn, "similar_days", project_id=p["id"])))
    rows = _text_rows(conn, f"i.project_id=? AND i.kind='work' AND (i.status IN {OPEN_STATES} "
                            f"OR (i.status='done' AND i.closed_at>=?))", (p["id"], since))
    skip = {int(x) for x in linked if x is not None} | _recent_batch(conn, actor)
    idf = _idf(conn)
    t = _words(title)
    q = _vector(t + t + sorted(set(_words(context))), idf)
    mine = _touch_keys(_touches(touches) or "", p["id"], p["path"])
    places = _places(_idents(f"{title} {context}")) if doer == "human" else set()
    common = _ident_counts([d for d in rows if d["status"] in OPEN_STATES]) if places else {}
    out = []
    for d in rows:
        if d["id"] in skip:
            continue
        s = _cosine(q, _vector(d["key"], idf))
        if mine and d["status"] in OPEN_STATES and d["touches"] and any(
                _keys_overlap(x, y) for x in mine for y in _touch_keys(d["touches"], d["project_id"], d["path"])):
            s += 0.15
        if places and d["doer"] == "human" and any(
                common.get(x, 0) < IDENT_COMMON for x in places & _item_idents(d)):
            s += IDENT_BONUS
        if s >= floor:
            out.append(_match(d, min(s, 1.0)))
    out.sort(key=lambda m: -m["score"])
    return out[:limit]


def _linked_ids(conn, item_id):
    ids = {r[0] for r in conn.execute("SELECT blocked_by FROM deps WHERE item_id=? UNION SELECT item_id FROM deps "
                                      "WHERE blocked_by=? UNION SELECT id FROM items WHERE found_during=? UNION "
                                      "SELECT found_during FROM items WHERE id=?", (item_id,) * 4)}
    return ids - {None, item_id}


def related(conn, item_id, n=3):
    """The items most like this one in its project, in any state (not the ones it is linked to already), so a
    worker sees earlier work before it starts."""
    it = _item(conn, item_id)
    skip = _linked_ids(conn, it["id"]) | {it["id"]}
    idf = _idf(conn)
    t = _words(it["title"])
    q = _vector(t + t + sorted(set(_words(it["context"]))), idf)
    scored = sorted(((_cosine(q, _vector(d["key"], idf)), d) for d in _text_rows(
        conn, "i.project_id=? AND i.kind='work'", (it["project_id"],)) if d["id"] not in skip), key=lambda x: -x[0])
    return [_match(d, s) for s, d in scored[:n] if s >= RELATED_MIN]


def duplicates(conn, project=None):
    """Groups of open items that match each other with the score of the add check (similar_min of the project),
    for the manager and the planner to merge (maxpm merge). Items linked to each other, and a series that one
    author added within two hours, are not duplicates. Two person items that name the same place score
    IDENT_BONUS more: a reworded ask for the same step (similar_items)."""
    names = _names(conn, project) if project else None
    rows = [d for d in _text_rows(conn, f"i.kind='work' AND i.status IN {OPEN_STATES}")
            if (names is None or d["project"] in names)]
    if not rows:
        return []
    idf = _idf(conn)
    vec = {d["id"]: _vector(d["key"], idf) for d in rows}
    ids = [d["id"] for d in rows]
    links = {(r[0], r[1]) for r in conn.execute(
        f"SELECT item_id, blocked_by FROM deps WHERE item_id IN ({','.join('?' * len(ids))})", ids)}
    links |= {(d["id"], d["found_during"]) for d in rows if d["found_during"]}
    added = {r["item_id"]: (r["actor"], parse_iso(r["at"])) for r in conn.execute(
        f"SELECT item_id, actor, at FROM events WHERE change LIKE 'added to %' "
        f"AND item_id IN ({','.join('?' * len(ids))})", ids)}
    common = _ident_counts(rows)
    places = {d["id"]: {x for x in _places(d["idents"]) if common[x] <= IDENT_COMMON}
              for d in rows if d["doer"] == "human"}
    floors = {}
    parent = {i: i for i in ids}
    root = lambda i: i if parent[i] == i else root(parent[i])
    pairs = []
    for k, a in enumerate(rows):
        f = floors.setdefault(a["project_id"], setting(conn, "similar_min", project_id=a["project_id"]))
        if f == "off":
            continue
        for b in rows[k + 1:]:
            if b["project_id"] != a["project_id"] or (a["id"], b["id"]) in links or (b["id"], a["id"]) in links:
                continue
            x, y = added.get(a["id"]), added.get(b["id"])
            if x and y and x[0] and x[0] == y[0] and abs(x[1] - y[1]) < SIMILAR_BATCH:
                continue
            s = _cosine(vec[a["id"]], vec[b["id"]])
            if a["id"] in places and b["id"] in places and places[a["id"]] & places[b["id"]]:
                s = min(s + IDENT_BONUS, 1.0)
            if s >= float(f):
                pairs.append((a["id"], b["id"], round(s, 2)))
                parent[root(b["id"])] = root(a["id"])
    by = {d["id"]: d for d in rows}
    groups = {}
    for a, b, s in pairs:
        groups.setdefault(root(a), {"items": set(), "pairs": []})
        groups[root(a)]["items"] |= {a, b}
        groups[root(a)]["pairs"].append({"a": a, "b": b, "score": s})
    out = [{"project": by[min(g["items"])]["project"], "items": [_match(by[i], 1) for i in sorted(g["items"])],
            "pairs": g["pairs"], "score": max(p["score"] for p in g["pairs"])} for g in groups.values()]
    for g in out:
        for m in g["items"]:
            del m["score"]
    return sorted(out, key=lambda g: (-g["score"], g["items"][0]["id"]))


def _append(old, other_id, text):
    old, text = (old or "").strip(), (text or "").strip()
    if not text or text in old:
        return old
    return (old + "\n\n" if old else "") + f"[from #{other_id}] {text}"


def merge(conn, keep, other, note, actor=None):
    """Close `other` as a duplicate of `keep`. Its notes, context, touches, tracker links, goal tags, messages,
    links (what it waits on, what waits on it), queue places, reservation, and found-during links move to `keep`;
    `keep` takes the higher priority and the earlier due date. A person's item merges only with a person's item."""
    if not (note or "").strip():
        raise RiverError(f"say why they are the same work: maxpm merge {keep} {other} --note \"<why>\"")
    keep, other = int(keep), int(other)
    if keep == other:
        raise RiverError("an item cannot merge into itself")
    moved = []
    with tx(conn):
        k, o = _item(conn, keep), _item(conn, other)
        for it in (k, o):
            if it["status"] not in OPEN_STATES:
                raise RiverError(f"#{it['id']} is {it['status']}; merge open items only (maxpm reopen {it['id']} first)")
            if it["kind"] != "work":
                raise RiverError(f"#{it['id']} is a {it['kind']} item; only work items merge")
        if (k["doer"] == "human") != (o["doer"] == "human"):
            raise RiverError(f"#{o['id']} is for {'a person' if o['doer'] == 'human' else 'an agent'} and #{k['id']} "
                             f"is not: a person's item merges only with a person's item. Link them instead: "
                             f"maxpm dep <id> --on <id>")
        if o["assignee"] and o["status"] in ("in_progress", "held") and o["assignee"] != actor:
            raise RiverError(f"#{o['id']} is held by {o['assignee']}; ask them to release it (maxpm ask "
                             f"{o['assignee']} \"...\" --item {o['id']}), or keep #{o['id']} and merge #{k['id']} into it")
        conn.execute("UPDATE items SET notes=?, context=?, touches=?, \"check\"=?, priority=?, due=? WHERE id=?",
                     (_append(k["notes"], o["id"], o["notes"]), _append(k["context"], o["id"], o["context"]),
                      _touches(touches_list(k["touches"]) + touches_list(o["touches"])) or "",
                      k["check"] or o["check"] or "", min(k["priority"], o["priority"]),
                      min(x for x in (k["due"], o["due"]) if x) if (k["due"] or o["due"]) else None, k["id"]))
        for r in conn.execute("SELECT ref FROM item_refs WHERE item_id=?", (o["id"],)).fetchall():
            if conn.execute("SELECT 1 FROM item_refs WHERE item_id=? AND ref=?", (k["id"], r["ref"])).fetchone():
                conn.execute("DELETE FROM item_refs WHERE item_id=? AND ref=?", (o["id"], r["ref"]))
            else:
                conn.execute("UPDATE item_refs SET item_id=? WHERE item_id=? AND ref=?", (k["id"], o["id"], r["ref"]))
                moved.append(r["ref"])
        conn.execute("INSERT OR IGNORE INTO item_goals(item_id, goal_id) SELECT ?, goal_id FROM item_goals "
                     "WHERE item_id=?", (k["id"], o["id"]))
        conn.execute("DELETE FROM item_goals WHERE item_id=?", (o["id"],))
        n = conn.execute("UPDATE messages SET item_id=? WHERE item_id=?", (k["id"], o["id"])).rowcount
        if n:
            moved.append(f"{n} message(s)")
        # Links: delete the other's, then add each one again on the kept item (a loop is left out and named).
        waits = conn.execute("SELECT blocked_by, kind FROM deps WHERE item_id=? AND auto=0", (o["id"],)).fetchall()
        waited = conn.execute("SELECT item_id, kind FROM deps WHERE blocked_by=? AND auto=0", (o["id"],)).fetchall()
        conn.execute("DELETE FROM deps WHERE item_id=? OR blocked_by=?", (o["id"], o["id"]))
        skipped = []
        for a, b, kind in [(k["id"], r["blocked_by"], r["kind"]) for r in waits] + \
                          [(r["item_id"], k["id"], r["kind"]) for r in waited]:
            if a == b or _link(conn, *sorted((a, b))):
                continue
            try:
                _dep_add(conn, a, b, actor, kind, alert=False)
                moved.append(f"#{a} waits on #{b}" if kind != "conflicts" else f"#{a} conflicts with #{b}")
            except RiverError:
                skipped.append(f"#{a} waits on #{b} (a loop)")
        for r in conn.execute("SELECT id, agent FROM queue_entries WHERE item_id=?", (o["id"],)).fetchall():
            if conn.execute("SELECT 1 FROM queue_entries WHERE item_id=? AND agent=?", (k["id"], r["agent"])).fetchone():
                conn.execute("DELETE FROM queue_entries WHERE id=?", (r["id"],))
            else:
                conn.execute("UPDATE queue_entries SET item_id=? WHERE id=?", (k["id"], r["id"]))
                moved.append(f"the queue place of {r['agent']}")
        if o["reserved_for"] and not k["reserved_for"] and k["status"] == "open":
            conn.execute("UPDATE items SET reserved_for=?, reserved_until=?, reserved_by=? WHERE id=?",
                         (o["reserved_for"], o["reserved_until"], o["reserved_by"], k["id"]))
            moved.append(f"the reservation for {o['reserved_for']}")
        conn.execute("UPDATE items SET found_during=? WHERE found_during=? AND id<>?", (k["id"], o["id"], k["id"]))
        if o["found_during"] and not k["found_during"] and o["found_during"] != k["id"]:
            conn.execute("UPDATE items SET found_during=? WHERE id=?", (o["found_during"], k["id"]))
        _sync_conflicts(conn, k["id"], actor)
        why = note.strip()
        if o["doer"] == "human" and _is_ai(conn, actor):
            _record_takeover(conn, o, "dropped", f"merged into #{k['id']}: {why}", actor)
        conn.execute("UPDATE items SET status='dropped', closed_at=?, assignee=NULL, lease_expires_at=NULL, "
                     "hold_expires_at=NULL, reserved_for=NULL, reserved_until=NULL, needs_check=0, backlog_at=NULL, "
                     "backlog_note=NULL, touches='', output=? WHERE id=?",
                     (iso(now()), f"duplicate of #{k['id']}: {why}", o["id"]))
        _sync_conflicts(conn, o["id"], actor)
        _event(conn, o["id"], actor, f"merged into #{k['id']} (a duplicate): {why}")
        _close_check(conn, _item(conn, o["id"]), "dropped", note=why, merged_into=k["id"])
        _event(conn, k["id"], actor, f"#{o['id']} {o['title']} merged into this item: {why}"
               + (f"; moved {', '.join(moved)}" if moved else ""))
        _goal_notice(conn, k["id"], actor, f"merged #{o['id']} into", why)
    res = item_show(conn, k["id"])
    res["merged"] = {"id": o["id"], "title": o["title"], "moved": moved, "skipped": skipped}
    res["close_check"] = _close_rows(conn, "c.closed_id=? AND c.decided_at IS NULL", (o["id"],))
    return res


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
        # A backlog item stays open, so the items that wait on it still wait; it is only never ready.
        backlog = bool(it["backlog_at"]) and it["status"] == "open"
        ready = it["status"] == "open" and not open_blockers and not it["blocked_reason"] and not busy and not backlog
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
            backlog=backlog,
            backlog_blockers=sorted(b for b in open_blockers if items[b]["backlog_at"] and items[b]["status"] == "open"),
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


def session_tree(conn, actor, ann=None):
    """The tree of a session (auto_continue tree, #4994): the open tasks linked to what it held since it registered,
    over waits-on and blocks links and found-during links in both directions, and the tasks of the goals of those
    tasks and of the goal it owns. None when the session held no task yet: it may take any work."""
    if not actor:
        return None
    start = conn.execute("SELECT MAX(id) FROM events WHERE item_id IS NULL AND actor=? AND change LIKE 'registered as%'",
                         (actor,)).fetchone()[0] or 0
    held = {r["item_id"] for r in conn.execute(
        "SELECT DISTINCT item_id FROM events WHERE actor=? AND id>? AND item_id IS NOT NULL AND change LIKE 'claimed%'",
        (actor, start))}
    held |= {r["id"] for r in conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held')",
                                           (actor,))}
    if not held:
        return None
    ann = ann if ann is not None else annotate(conn)
    found = {}
    for r in conn.execute("SELECT id, found_during FROM items WHERE found_during IS NOT NULL"):
        found.setdefault(r["found_during"], set()).add(r["id"])
        found.setdefault(r["id"], set()).add(r["found_during"])
    goals = {g for i in held if i in ann for g in ann[i]["goals"]}
    goals |= {r["name"] for r in conn.execute("SELECT name FROM goals WHERE owner=? AND status='open'", (actor,))}
    tree, frontier = set(held), list(held)
    while frontier:
        nxt = []
        for x in frontier:
            near = (ann[x]["waits_on"] + ann[x]["unblocks"] if x in ann else []) + list(found.get(x, ()))
            for y in near:
                if y not in tree:
                    tree.add(y)
                    nxt.append(y)
        frontier = nxt
    tree |= {a["id"] for a in ann.values() if goals & set(a["goals"])}
    return {i for i in tree if i in ann and ann[i]["status"] in OPEN_STATES}


def _in_tree(item_id, tree):
    """Whether a session may get this item: it has no tree (None: any work), or the item is in it."""
    return tree is None or item_id in tree


def tree_mode(conn, actor):
    """The tree of this session when auto_continue is tree and it held a task already; else None (any work)."""
    if not actor or setting(conn, "auto_continue", agent=actor) != "tree" or not _is_ai(conn, actor):
        return None
    return session_tree(conn, actor)


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
                              and not a["backlog"] and a["conflict_holders"] == [actor])


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
    if actor:
        # An expert-tier task goes to a session started for it (launch --item, maxpm expert start), not to a worker
        # that asks for work (#4959): only the session it is reserved for sees it.
        pool = [a for a in pool if a.get("tier") != "expert" or a["reserved_for"] == actor]
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
        _refuse_backlog(it, "queued")
        if ag["kind"] != "ai":
            raise RiverError(f"{agent} is a person; a queue is for an agent session")
        if it["doer"] == "human":
            raise RiverError(f"#{it['id']} is for a person")
        why = reviewer_refusal(conn, it, agent)
        if why:
            raise RiverError(f"refused: {agent}: {why}. Queue it for a session that held no task and did not work "
                             f"on the release")
        _refuse_model_to(conn, it, agent, "queue")
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


def proc_cwd(pid):
    """The working folder of process pid on this computer, or None (#5262): a session's folder when its chat log does
    not name it (Grok, Copilot). Linux: /proc/<pid>/cwd; macOS: lsof; Windows: None."""
    import subprocess
    if not pid or PLATFORM == "win32":
        return None
    link = f"/proc/{int(pid)}/cwd"
    if os.path.exists(link):
        try:
            return os.readlink(link)
        except OSError:
            return None
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(int(pid)), "-d", "cwd", "-Fn"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return next((x[1:] for x in out.splitlines() if x.startswith("n/")), None)


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


def proc_tty(pid):
    """The terminal of a process ('ttys030'); '' when it runs on none (a background process: ps shows ?? or ?);
    None when ps cannot tell (no ps, no such process, or Windows)."""
    import subprocess
    if os.name == "nt":
        return None
    try:
        out = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    t = out.stdout.strip()
    if out.returncode or not t:
        return None
    return "" if not t.strip("?") else t


def process_mode(conn, name, env=None):
    """IDENTITY['process'] of the agent of `name`: of the adapter this command runs inside, else of the agent's type;
    'ancestor' by default."""
    a = _adapters().from_env(os.environ if env is None else env) or state_adapter(conn, name)
    return ((a.identity or {}).get("process") if a else None) or "ancestor"


def process_on_terminal(conn, name, env=None):
    """Whether the session's own process of `name` must run on a terminal: its adapter's IDENTITY['process'] is
    'terminal' (#5247: Hermes) or 'nearest_terminal' (#5283: OpenCode)."""
    return process_mode(conn, name, env) in ("terminal", "nearest_terminal")


def find_agent_process(conn, name, start=None, env=None):
    """agent_process with the rule of the agent's adapter (IDENTITY['process'])."""
    mode = process_mode(conn, name, env)
    if mode == "terminal":
        return agent_process(start, terminal=True)
    if mode == "nearest_terminal":
        return agent_process(start, climb=True)
    return agent_process(start) if start else agent_process()


def drop_background_process(conn, name):
    """Forget the recorded process of `name` when it runs on no terminal (#5247): a Hermes gateway, shared by every
    Hermes session, that an older MaximizePM recorded as the session's own. A process ps cannot read stays."""
    r = conn.execute("SELECT pid, host FROM agents WHERE name=?", (name,)).fetchone()
    if r and r["pid"] and r["host"] == this_host() and proc_tty(r["pid"]) == "":
        with tx(conn):
            conn.execute("UPDATE agents SET pid=NULL, pid_cmd=NULL WHERE name=? AND pid=?", (name, r["pid"]))
            _event(conn, None, name, f"{name}: process {r['pid']} runs on no terminal, so it is no session's own; "
                                     f"forgotten")
        return True
    return False


def agent_process(start=None, terminal=False, climb=False):
    """The agent CLI process that runs this river command: the first ancestor above the shell(s) that is not
    a shell (SHELLS). Returns (pid, command line), or (None, None) when it finds none (no ps, or on Windows a
    terminal that a person works in). terminal: that process counts only when it runs on a terminal
    (process_on_terminal, #5247): a Hermes command runs under the gateway that every Hermes session shares.
    climb: go on upward to the first process that runs on a terminal (#5283: an OpenCode command runs under the
    TUI's private server, which has none; the TUI is the session's own process)."""
    pid = start or os.getppid()
    for _ in range(12):
        info = proc_info(pid)
        if info is None:
            return None, None
        ppid, comm, args = info
        if not _between(comm, args):
            if os.name == "nt" and re.sub(r"\.exe$", "", comm.lower()) in WIN_HOSTS:
                return None, None
            if (terminal or climb) and os.name != "nt" and not proc_tty(pid):
                if climb and ppid > 1:
                    pid = ppid
                    continue
                return None, None
            return pid, args
        if ppid <= 1:
            return None, None
        pid = ppid
    return None, None


def set_process(conn, name, pid, cmd, host=None, env=None):
    """Record the agent CLI process of this agent. A process is one agent's only (#5060): a command with --as for
    another agent, run in the session of the manager (its MAXPM_AGENT names the manager), wrote the manager's pid
    into that agent. who then showed a dead expert as active, a resume refused, and tmux_adopt ended the manager.
    So the session that MAXPM_AGENT names records only its own name, and takes its pid back from rows that hold it.
    With no MAXPM_AGENT, a process (the same pid and command) that another agent of this host holds stays that
    agent's; a row with the pid and another command is an old process whose pid came again, and loses it. Returns
    whether it recorded."""
    me = (os.environ if env is None else env).get("MAXPM_AGENT")
    if me and me != name:
        return False
    host = host or this_host()
    with tx(conn):
        if not me and conn.execute("SELECT 1 FROM agents WHERE pid=? AND host=? AND name!=? AND pid_cmd IS ?",
                                   (pid, host, name, cmd)).fetchone():
            return False
        conn.execute("UPDATE agents SET pid=NULL, pid_cmd=NULL WHERE pid=? AND host=? AND name!=?", (pid, host, name))
        conn.execute("UPDATE agents SET pid=?, pid_cmd=?, host=? WHERE name=?", (pid, cmd, host, name))
    return True


def kill_agent(conn, agent, reason, actor=None, grace=5.0, sleep=None, pane=None):
    """Emergency only (the normal path is maxpm stop): end the agent's process on this host, then release
    everything it held. Checks that the PID still runs the recorded command, so a reused PID is not killed.
    pane: for an agent with no recorded process (it ran no maxpm command: Aider, a session that never connected), the
    foreground process group of its tmux pane as maxpm serve found it ({"pid", "pgid", "cmd", "pane"},
    server.pane_process); the whole group ends, a pipeline such as 'printf <prompt> | amp' too (#5278)."""
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
    if not ag["pid"] and pane:
        return _kill_pane(conn, agent, reason, actor, pane, grace, sleep)
    if not ag["pid"]:
        raise RiverError(f"{agent} has no recorded process (maxpm go records it) and no tmux pane of this queue runs "
                         f"it; ask it to stop instead: maxpm stop {agent} --reason \"...\"")
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
    return _killed(conn, agent, reason, actor, ag["pid"], killed)


def _kill_pane(conn, agent, reason, actor, pane, grace, sleep):
    """kill_agent for the foreground process group of the agent's tmux pane (pane: server.pane_process)."""
    import signal
    pid, group = int(pane["pid"]), int(pane["pgid"])
    if os.name == "nt" or group <= 1 or group == os.getpgrp():
        raise RiverError(f"refused: process group {group} is not one MaximizePM may end")
    killed = False
    if pid_alive(pid):
        info = proc_info(pid)
        if info is not None and pane.get("cmd") and info[2] != pane["cmd"]:
            raise RiverError(f"refused: PID {pid} now runs another command ({info[2][:80]}), not {agent}'s; "
                             f"nothing was killed")
        try:
            os.killpg(group, signal.SIGTERM)
            waited = 0.0
            while pid_alive(pid) and waited < grace:
                sleep(0.2)
                waited += 0.2
                try:
                    os.waitpid(pid, os.WNOHANG)  # a child of this process (tests): reap it
                except (ChildProcessError, OSError):
                    pass
            if pid_alive(pid):
                os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass  # it ended by itself in the meantime
        killed = True
    with tx(conn):
        _event(conn, None, actor, f"{agent} had no recorded process: killed the foreground of its tmux pane "
               f"{pane.get('pane', '?')} (PID {pid}: {str(pane.get('cmd') or '')[:80]})")
    return {**_killed(conn, agent, reason, actor, pid, killed), "pane": pane.get("pane")}


def _killed(conn, agent, reason, actor, pid, killed):
    """The end of kill_agent: release everything the killed agent held, and mark it stopped."""
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
    return {"agent": agent, "pid": pid, "killed": killed, "released": held, "queued_back": queued,
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


def _stop_text(conn, agent, sender, reason):
    """The stop request that goes into a session (#5249): the exact commands for each item it holds, then go, which
    ends it. A Codex session that got only 'inbox; go' answered in words and kept its item."""
    held = [r["id"] for r in conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held') "
                                          "ORDER BY id", (agent,))]
    me = f"maxpm --as {agent}"
    steps = "".join(f" For #{i}: {me} done {i} --output \"<what changed, commit>\" when it is finished, else {me} "
                    f"release {i} --note \"<what is done, what is left>\"." for i in held)
    return (f"[maxpm stop request from {sender}] {reason} Commit the work that is finished.{steps} Then run {me} go "
            f"once more: with nothing held it ends this session. Answering in words does not end it.")


def _deliver_entry(conn, entry_id):
    r = conn.execute("SELECT * FROM queue_entries WHERE id=?", (entry_id,)).fetchone()
    st = deliver_native(conn, r["agent"], _stop_text(conn, r["agent"], r["added_by"] or "maxpm", r["body"])
                        if r["kind"] == "stop" else _native_text("instruction", r["added_by"] or "maxpm", r["body"]))
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


def end_stopped(conn, agent):
    """Finish the stop of an agent that ended its turn without finishing it (#5249: a Codex session answered 'Stopped
    as requested.' and ran no maxpm command, so it kept its item and its row said 'Ending' for half an hour).
    maxpm serve calls it when the agent sits idle at its prompt after the stop request (server.fresh_sessions):
    each item it still holds goes back to the queue with a note and CHECK FIRST (its commits may hold part of the
    work), its goals, targets and queue go as in _finish_stop, the senders of what it did not read hear it, and it
    is unregistered. Returns {"ended", "why", "released"}, or None when it has no stop request."""
    a = conn.execute("SELECT stop_at, stop_by, stop_reason FROM agents WHERE name=?", (agent,)).fetchone()
    if a is None or not a["stop_at"]:
        return None
    why = (f"stopped by {a['stop_by'] or 'maxpm'} ({a['stop_reason']}); its turn ended without the release, so "
           f"MaximizePM finished the stop")
    released = []
    for r in conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held') ORDER BY id",
                          (agent,)).fetchall():
        release(conn, r["id"], f"{agent} was {why}. CHECK FIRST: its commits (git log --grep '#{r['id']}') and "
                               f"files may hold part of the work", agent)
        with tx(conn):
            conn.execute("UPDATE items SET needs_check=1 WHERE id=? AND status='open'", (r["id"],))
        released.append(r["id"])
    _finish_stop(conn, agent)
    out = end_idle(conn, agent, why, force=True)
    return {**(out or {"ended": None, "why": why}), "released": released}


# ---------------------------------------------------------------- registry and leases

def _touch_agent(conn, actor):
    """Renew every lease and target ownership the actor holds, and record that it was seen."""
    if not actor:
        return
    t = now()
    conn.execute("UPDATE agents SET last_seen=? WHERE name=?", (iso(t), actor))
    # A command of a session that reported its end (maxpm release): it runs again (a resume), so it is not gone.
    conn.execute("UPDATE agents SET activity_event=NULL WHERE name=? AND activity_event=?", (actor, REPORT_GONE))
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
    experts = ({x["name"] for x in conn.execute("SELECT name FROM agents WHERE role IN ('expert', 'assistant')")}
               if expired else set())
    for r in expired:
        if r["assignee"] in experts:
            # An expert holds its task while the person talks with it, for days (#4959): no lease runs out. The same
            # for an item that an assistant took over with the person's yes (#5110).
            conn.execute("UPDATE items SET lease_expires_at=? WHERE id=?",
                         (iso(now() + _lease_for(conn, r["id"], r["assignee"])), r["id"]))
            continue
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
                          f"AND status IN {OPEN_STATES} AND backlog_at IS NULL AND due_warned < 2").fetchall():
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


def busy_now(conn, names=None, skip=()):
    """The agents (among names; else every AI agent) in whose session a command runs now: the agent CLI process
    runs on this computer, and a process below it started after the agent's last river command (a test run, a
    build, a merge). The servers the CLI started with are older, and so is a maxpm wait, which renews last_seen
    itself. On macOS Claude Code also runs `caffeinate -i -t 300` below it while a turn runs (it restarts it every
    4 minutes and ends it 30 seconds after the turn): that counts too, and rightly, since the session works; at
    its prompt it has none (#4294). Only within busy_max of the last river command. The processes in skip (the
    hook that asks, #4293) and the ones below them do not count. One ps call; an empty set with no ps (Windows, a sandbox that blocks it)."""
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
            if pid in seen or pid in skip:
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


def poll_activity(conn, actor, watch=False):
    """activity for a command that polls every few seconds (maxpm wait, maxpm manage --watch, the state read
    of the page). Each poll only records that the actor is there and renews its leases, in one short
    transaction; the sweep and sync_needs_you run each POLL_SWEEP, because every ordinary command and maxpm
    serve run them too (#1617: ten polls held the write lock a large part of the time). When the queue stays
    locked for one busy_timeout, the poll skips this renewal and the next one tries again: a lock never ends
    a wait (#1615). watch: the poll of a manager's watch, which also records that the watch runs (#1898)."""
    key, t = conn.execute("PRAGMA database_list").fetchone()[2], now()
    last = _POLL_SWEPT.get(key)
    out = []
    try:
        sweep = last is None or not timedelta(0) <= t - last < POLL_SWEEP
        if sweep:
            out = activity(conn, actor, tries=1)
            _POLL_SWEPT[key] = t
        if actor and (watch or not sweep):
            with tx(conn, 1):
                if not sweep:
                    _touch_agent(conn, actor)
                if watch:
                    conn.execute("UPDATE agents SET watch_seen=? WHERE name=?", (iso(t), actor))
    except RiverLocked:
        pass
    return out


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


def origin_agent(conn, item_id=None, message_id=None):
    """The name of who asked (a message) or added the item (its first event), if known."""
    r = None
    if message_id is not None:
        r = conn.execute("SELECT from_agent who FROM messages WHERE id=?", (message_id,)).fetchone()
    elif item_id is not None:
        r = conn.execute("SELECT actor who FROM events WHERE item_id=? ORDER BY id LIMIT 1", (item_id,)).fetchone()
    return r["who"] if r else None


def origin_session_url(conn, item_id=None, message_id=None):
    """The session link of the agent that asked (a message) or added the item (its first event), if known."""
    who = origin_agent(conn, item_id, message_id)
    r = conn.execute("SELECT session_url FROM agents WHERE name=?", (who,)).fetchone() if who else None
    return r and r["session_url"]


CODEX_WEB = "https://chatgpt.com/codex"


def agent_web_url(conn, name):
    """The agent's own app on the web, for a notification about its work (notify_open agent, #4673): the session
    link it recorded (Claude Code with Remote Control: claude.ai/code/<session>), else Codex's web app (a Codex
    session records no task link), else None (the caller opens the MaximizePM page)."""
    r = conn.execute("SELECT kind, session_url, agent_type FROM agents WHERE name=?", (name,)).fetchone() if name else None
    if not r or r["kind"] != "ai":
        return None
    if r["session_url"]:
        return r["session_url"]
    ad = _adapters().find(r["agent_type"]) if r["agent_type"] else None
    url = (ad.identity or {}).get("url") if ad else None
    return url if url and "{id}" not in url else None  # an app with no link for each session (Codex: CODEX_WEB)


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
    _release_held(conn)
    ann = annotate(conn)
    _hold_items(conn, ann)
    want_items = {a["id"]: a for a in ann.values()
                  if a["ready"] and a["doer"] == "human" and not a["project_archived"] and not a["held_for"]}
    want_msgs = {}
    if humans:
        # A question held for the manager (#5307) is not the person's yet.
        for r in conn.execute(
                f"SELECT * FROM messages WHERE kind IN ('question','alert') AND to_agent IN ({','.join('?' * len(humans))}) "
                "AND held_for IS NULL AND ((kind='question' AND state='open') OR (kind='alert' AND read_at IS NULL))",
                sorted(humans)):
            want_msgs[r["id"]] = r
    t = iso(now())
    for ev in conn.execute("SELECT * FROM needs_you WHERE closed_at IS NULL").fetchall():
        if ev["kind"] == "item" and ev["item_id"] not in want_items:
            it = ann.get(ev["item_id"])
            why = (it["status"] if it and it["status"] in CLOSED_STATES
                   else "claimed" if it and it["status"] in ("in_progress", "held")
                   else "moved to the backlog" if it and it["backlog"]
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
        # A prompt alert names the window, the question, and the keys (server.watch_prompts): all of it, or the
        # person still cannot act on it from the notification (#2713).
        cut = 600 if m["body"].startswith(PROMPT_NOTE) else 120
        fwd = forward_note(conn, m) if m["kind"] == "question" else None
        _open_event(conn, "message", f"{m['kind']} from {m['from_agent']}: {m['body'][:cut]}"
                    + (f" (the manager: {fwd[:200]})" if fwd else ""),
                    item_id=m["item_id"], message_id=mid, human=m["to_agent"])


def forward_note(conn, q):
    """What the manager added when it forwarded question q to the person (#5307): its note in the thread, or None."""
    r = conn.execute("SELECT m.body FROM messages m JOIN agents a ON a.name=m.from_agent AND a.role='manager' "
                     "WHERE m.reply_to=? AND m.kind='note' AND m.to_agent=? ORDER BY m.id DESC LIMIT 1",
                     (q["id"], q["to_agent"])).fetchone()
    return r["body"] if r else None


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
    # Its session reported its end (maxpm release, #5167): gone at once, as when its process ends.
    if "activity_event" in a.keys() and a["activity_event"] == REPORT_GONE:
        return "gone"
    # An expert talks with a person and runs few maxpm commands: it is not away or gone by time. It ends after
    # expert_idle_end with no talk (end_quiet_experts), or when a person ends it (#4955). An assistant (#5110) goes
    # through the person's list with them, for as long as they talk: only the person ends it. Its process says when it
    # ended; with no process known (it never ran maxpm go), time says it, so a session that never started is gone.
    if "role" in a.keys() and (a["role"] == "expert" or (a["role"] == "assistant" and a["pid"])):
        return "active"
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
# The watch of a manager (maxpm manage --watch) records each poll and its return in agents.watch_seen (#1898).
# On 2026-10-09 a manager ended a turn and did not start its watch again: nothing wakes a session at its prompt,
# so for 16 minutes it heard of nothing, and a blocked question waited 11.9 minutes while its sender stood still.
# A watch runs when its last poll is younger than WATCH_BEAT (a poll comes each 3s; a locked queue skips some).
# After that, each command of the manager says that no watch runs. After WATCH_LATE with no watch and a message
# that is due, maxpm serve tells each person (tell_watch_late): the first words of that alert are WATCH_NOTE.
WATCH_BEAT = timedelta(minutes=1)
WATCH_LATE = timedelta(minutes=5)
WATCH_NOTE = "runs no watch"


def prompt_alert_agent(conn, message_id):
    """The agent whose terminal this alert is about (a prompt that waits there, or a manager that runs no
    watch: a person answers both in that terminal), or None for any other message."""
    r = conn.execute("SELECT from_agent, body FROM messages WHERE id=? AND kind='alert'", (message_id,)).fetchone() \
        if message_id is not None else None
    return r["from_agent"] if r and r["body"].startswith((PROMPT_NOTE, WATCH_NOTE)) else None


def not_connected(conn, a):
    """A session river started for an item that has run no river command since, for longer than connect_within."""
    return (a["kind"] == "ai" and (a["note"] or "").startswith(STARTED_NOTE) and a["last_seen"] == a["registered_at"]
            and not a["stop_at"] and now() - parse_iso(a["registered_at"]) > parse_duration(setting(conn, "connect_within")))


def agent_status(conn, name):
    a = dict(_agent(conn, name))
    a["state"] = _agent_state(conn, a)
    a["can_kill"] = bool(a.get("pid")) and a.get("host") == this_host() and a["state"] != "gone"
    a["holds"] = [dict(r) for r in conn.execute(
        "SELECT id, title, short, status, claimed_at, lease_expires_at, hold_expires_at FROM items WHERE assignee=? "
        "AND status IN ('in_progress','held') ORDER BY id",
        (name,))]
    a["owns"] = [dict(r) for r in conn.execute(
        "SELECT name, owner_expires_at FROM targets WHERE owner=? ORDER BY name", (name,))]
    a["pinned_by"] = [r["person"] for r in conn.execute(
        "SELECT person FROM session_pins WHERE agent=? ORDER BY pinned_at, person", (name,))]
    return a


def session_pin(conn, agent, actor=None, on=True):
    """A person pins a session to the top of the worker sessions on the Sessions tab, or unpins it (#5007). The pin is
    the person's (the page shows their own pins first), stays across reloads and restarts of maxpm serve, and goes
    when the session ends (its agent row goes)."""
    if not actor:
        raise RiverError("a pin is a person's: pass --as <your name>")
    who = conn.execute("SELECT kind FROM agents WHERE name=?", (actor,)).fetchone()
    if who is None or who["kind"] != "human":
        raise RiverError(f"a pin is a person's, and {actor} is not a registered person: pass --as <your name> "
                         f"(maxpm register <name> --human)")
    _agent(conn, agent)
    with tx(conn):
        if on:
            conn.execute("INSERT OR IGNORE INTO session_pins (person, agent, pinned_at) VALUES (?,?,?)",
                         (actor, agent, iso(now())))
        else:
            conn.execute("DELETE FROM session_pins WHERE person=? AND agent=?", (actor, agent))
    return {"agent": agent, "person": actor, "pinned": on,
            "pinned_by": agent_status(conn, agent)["pinned_by"]}


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
    if ag["role"] == "assistant" and it0["takeover_by"] != actor:
        raise RiverError(f"refused: {actor} is an assistant session: it goes through what waits on "
                         f"{ag['assistant_for'] or 'the person'}, with them, and takes no queue work. Add the work "
                         f"(maxpm add \"<title>\" --found-during <id>): a worker session takes it. A person's item that "
                         f"you do yourself, with their yes: maxpm --as {actor} takeover <id> --note \"<how>\"")
    if ag["role"] in ("planner", "manager"):
        raise RiverError(f"refused: {actor} is a {ag['role']} session; it changes the plan and does not take work. "
                         f"To work instead, run: maxpm --as {actor} go" + (
                             f". #{item_id} is reserved for you: start a session for it (maxpm launch --item {item_id}), "
                             f"or open it to every agent (maxpm edit {item_id} --unreserve)"
                             if it0["reserved_for"] == actor and it0["status"] == "open" else ""))
    if it0["backlog_at"] and it0["status"] == "open":
        raise RiverError(_backlog_claim_refusal(it0))
    if it0["tier"] == "expert" and ag["kind"] == "ai" and ag["role"] != "expert" and it0["reserved_for"] != actor:
        raise RiverError(f"refused: #{item_id} is an expert-tier task: a person talks it through with a session of its "
                         f"own, so a worker does not take it. A new session starts for it: maxpm launch --item {item_id}, "
                         f"or maxpm expert start <name> --item {item_id}")
    why = deploy_hook_refusal(conn, it0, actor) or (approval_refusal(conn, it0) if ag["kind"] == "ai" else None)
    if why:
        raise RiverError(f"refused: {why}. Take other work: maxpm go")
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
    # A goal's items are its owner's, unless one was pushed or given to this agent, or a person or the manager put it
    # in this agent's own queue: the queue wins, as annotate says (wait said WORK while claim refused, #5089).
    if ag["kind"] == "ai" and it0["doer"] != "human" and it0["reserved_for"] != actor and not (q and q["agent"] == actor):
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
                         f"model or agent type takes it; the manager can change them: maxpm edit {item_id} "
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
    if ag["model"]:  # the model the work runs on: an expert that takes it up takes the same (#4961, #4979)
        conn.execute("UPDATE items SET ran_model=? WHERE id=?",
                     (ladder_short(parse_ladder(setting(conn, "model_ladder")), ag["model"]) or ag["model"], item_id))
    if it0["kind"] in ("review", "deploy"):
        _cut_on_claim(conn, it0, actor)  # the release holds a fixed list of items from now on
    if conn.execute("DELETE FROM queue_entries WHERE item_id=?", (item_id,)).rowcount:
        _event(conn, item_id, actor, "left the queue (claimed)")
    _goal_notice(conn, item_id, actor, "claimed")
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
        ok, why = model_check(parse_ladder(ladder_text), model, m["min_model"], m["max_model"],
                              session_family(conn, actor, stype))
    return None if ok else why


def _refuse_model_to(conn, it, to, verb):
    """Refuse to hand an item to a session whose model the item's min or max limit excludes: a push, a give,
    or a queue entry would hold the item for a session that may not claim it (#4950). The agent type is not
    checked here: a person may push an item to a session of another type, and that session takes it."""
    why = _model_refusal(conn, it, to, any_type=True)
    if why:
        raise RiverError(f"refused: {verb} #{it['id']} to {to}: {why}. Choose a session whose model fits, or "
                         f"change the limit: maxpm edit {it['id']} --min-model|--max-model <model>")


def next_item(conn, project=None, unblocks=None, claim=False, actor=None, limit=1, near=None, mine=False,
              model=None, skipped=None, folderless=False, only=None):
    """Show, or claim, the first ready item from the area the agent chose.

    model: the session's model (default: the one the agent recorded). Items whose min/max limits exclude it
    are left out; `skipped`, a list, receives them with the reason. folderless (a chat app session): items
    that need a folder are left out too (needs_folder)."""
    model = model or agent_model(conn, actor)
    ladder = parse_ladder(setting(conn, "model_ladder")) if model else None
    stype = session_type(conn, actor)
    family = session_family(conn, actor, stype)
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
                ok, why = model_check(ladder, model, a["min_model"], a["max_model"], family)
            if ok:
                a["model_note"] = why
                out.append(a)
            elif skipped is not None and a["id"] not in {x["id"] for x in skipped}:
                skipped.append({"id": a["id"], "title": a["title"], "why": why})
        return out

    def takeable(pool):
        if only is not None:  # auto_continue tree: the session's own tree (session_tree, #4994)
            pool = [a for a in pool if a["id"] in only]
        if not actor:
            return fits(pool)
        owned = {r["name"] for r in conn.execute("SELECT name FROM targets WHERE owner=?", (actor,))}
        # An item an answer just came for goes to a fresh session (maxpm serve starts it within a pass or two),
        # not to a session that waits; with no maxpm serve, the next go takes it after FRESH_GRACE.
        fresh = {r["id"] for r in conn.execute("SELECT id FROM items WHERE fresh_start > ?",
                                               (iso(now() - FRESH_GRACE),))}
        if _is_ai(conn, actor):
            owned -= hooked_targets(conn)  # maxpm serve runs the release of a target with a deploy hook
        pool = [a for a in pool if (a["kind"] != "deploy" or a["target"] in owned)
                and a["reserved_for"] in (None, actor) and a["id"] not in fresh]
        # An item whose lease ran out while its agent's session still works on it: no second agent takes it.
        still = still_worked(conn)
        for a in [a for a in pool if still.get(a["id"], actor) != actor]:
            if skipped is not None and a["id"] not in {x["id"] for x in skipped}:
                skipped.append({"id": a["id"], "title": a["title"], "why": f"{still[a['id']]} still works on it in its session"})
        pool = [a for a in pool if still.get(a["id"], actor) == actor]
        for a in [a for a in pool if a["kind"] in FRESH_KINDS and reviewer_refusal(conn, a, actor)]:
            if skipped is not None and a["id"] not in {x["id"] for x in skipped}:
                skipped.append({"id": a["id"], "title": a["title"],
                                "why": "you worked on the release it reviews" if author_refusal(conn, a, actor)
                                else f"{'a review' if a['kind'] == 'review' else 'an operational task'} starts in "
                                     f"a new session; this one held a task"})
            pool.remove(a)
        # The agent's own queue comes first, in its order and from any project; then items pushed to it.
        # The sort is stable, so graph order holds inside each group.
        qpos = {r["item_id"]: n for n, r in enumerate(conn.execute(
            "SELECT item_id FROM queue_entries WHERE agent=? AND item_id IS NOT NULL ORDER BY pos", (actor,)))}
        have = {a["id"] for a in pool}
        pool = [a for a in _queue_ready(conn, actor) if a["id"] not in have and not reviewer_refusal(conn, a, actor)] + pool
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


def _backlog_claim_refusal(it):
    return (f"refused: #{it['id']} is in the backlog (nobody plans to take it now"
            + (f": {it['backlog_note']}" if it["backlog_note"] else "")
            + f"). Take other work (maxpm go); a person or the manager makes it open again: maxpm activate {it['id']}")


def claim(conn, item_id, actor=None):
    _prime_release_commits(conn, item_id)
    with tx(conn, claims=True):
        _sweep(conn)
        a = annotate(conn)[_item(conn, item_id)["id"]]
        if a["backlog"]:
            raise RiverError(_backlog_claim_refusal(a))
        if not ready_for(a, actor):
            why = (f"waits on {', '.join('#' + str(b) for b in a['open_blockers'])}" if a["open_blockers"]
                   else a["blocked_text"] if a["blocked_reason"]
                   else f"conflicts with {', '.join('#' + str(b) for b in a['busy_conflicts'])}, which is in progress "
                        f"(both edit the same files)" if a["busy_conflicts"] and a["status"] == "open"
                   else f"status is {a['status']}" + (f" (held by {a['assignee']})" if a["assignee"] else ""))
            raise RiverError(f"item {item_id} is not ready: {why}. See: maxpm blockers {item_id}")
        why = reviewer_refusal(conn, a, actor)
        if why:
            raise RiverError(f"refused: {why}. Take other work: maxpm go")
        first = still_worked(conn).get(a["id"], actor)
        if first != actor:
            raise RiverError(f"refused: the lease on #{item_id} ran out, but {first} still works on it in its session "
                             f"(a command runs there). Take other work (maxpm go). A person or a manager ends that "
                             f"session first: maxpm stop {first} --reason \"...\"")
        _claim_row(conn, a["id"], actor)
        it = _item(conn, a["id"])
        if it["tier"] == "expert" and _agent(conn, actor)["kind"] == "ai":
            # The session that takes an expert-tier task is an expert from its start (#4959).
            _make_expert(conn, actor, it["title"], _project_name(conn, it["project_id"]), actor)
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
                     "reserved_for=NULL, needs_check=0, backlog_at=NULL, backlog_note=NULL, output=COALESCE(?, output) "
                     "WHERE id=?", (status, iso(now()), output, it["id"]))
        _event(conn, it["id"], actor, status + (f": {output}" if output else f": {note}" if note else ""))
        unneeded = _close_check(conn, it, status, output, note) if it["kind"] == "work" else []
        if status == "done" and it["kind"] == "deploy":
            _hook(conn, "deployed", it["target"], it["id"], actor)
        elif status == "done" and it["kind"] == "review":
            _hook(conn, "review-passed", it["target"], it["id"], actor, _review_deploy(conn, it["id"]))
        goal_checks = _ask_done_when(conn, conn.execute(
            "SELECT g.* FROM item_goals ig JOIN goals g ON g.id=ig.goal_id WHERE ig.item_id=? ORDER BY g.rank, g.id",
            (it["id"],)).fetchall(), actor, f"#{it['id']} {it['title']}", send=False)
        _goal_notice(conn, it["id"], actor, "finished" if status == "done" else status, output or note,
                     ask={x["goal"]: x["question"] for x in goal_checks if x["asked"] not in ("you", "manager")})
        newly, resumed = [], []
        if status in CLOSED_STATES:
            resumed = _resume_holds(conn, it["id"])
            ann = annotate(conn)
            newly = [d for d in ann[it["id"]]["unblocks"] if ann[d]["ready"]]
            # An item that waited on an item nobody needs now is ready only because of the close: its real
            # precondition may still be false (#3292). The close check says so, and the closer relinks it.
            if unneeded:
                for d in newly:
                    conn.execute("UPDATE close_checks SET why=why || ?, ready=1 WHERE closed_id=? AND item_id=? "
                                 "AND ready=0", ("; the close makes it ready", it["id"], d))
                unneeded = _close_rows(conn, "c.closed_id=? AND c.decided_at IS NULL", (it["id"],))
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
    res["close_check"] = unneeded
    res["goal_checks"] = goal_checks
    if status == "done" and _is_ai(conn, actor):
        # A shared goal has no owner who keeps its handoff: the worker that finished a task of it does (#4974).
        res["shared_handoffs"] = [r["name"] for r in conn.execute(
            "SELECT g.name FROM item_goals ig JOIN goals g ON g.id=ig.goal_id WHERE ig.item_id=? AND g.shared=1 "
            "AND g.status='open' ORDER BY g.rank, g.id", (it["id"],))]
    return res


_NOT_NEEDED_RE = re.compile(r"\b(no longer (needed|necessary|wanted|relevant)|not needed|unnecessary|obsolete|"
                            r"superseded|moot)\b", re.I)
_REVERT_RE = re.compile(r"\brevert", re.I)


def _close_check(conn, it, status, output=None, note=None, merged_into=None, before=None):
    """Inside a tx, after the close of a work item: the open items that the close may have made unnecessary
    (#3286), recorded in close_checks for the closer, maxpm cleanup, and the manager. First the linked ones:
    - the item is not needed (dropped, merged, or done with a note that says so): what waits on it, and what was
      found during it;
    - done although it waited on open items (--force): those items;
    - a revert (its title or output says revert and names #N): the person's items found during #N.
    Then open items of the same folder that name one of its identifiers, when the score with IDENT_BONUS reaches
    CLOSE_NEAR: IDENT_BONUS for a place (_places: a URL, path, quoted name, or uuid) that its title, output, or
    note names, half of that for another identifier there (an item number, a commit id, a mail address: a look
    at the result names the commit, follow-ups of one item name it) or one in its context. Without an
    identifier, the words alone must reach similar_min (#3291: on the live queue every word-only match below
    about 0.5 was noise, and the same goal told nothing). At most CLOSE_LIMIT, person items first.
    Left out of the score: a series (one author added it with the item within SIMILAR_BATCH, or its title has
    the same words before the colon: 'Onboarding test: <app>'), and an item kept after an earlier close, unless
    it names an identifier. What waits on a done item, and what was found during it, are its next steps: they
    are not listed. before: leave out items made after that time (a check of a past close)."""
    cid = it["id"]
    output = output if output is not None else it["output"]
    said = " ".join(x or "" for x in (it["title"], output, note))
    gone = status == "dropped" or merged_into is not None or bool(_NOT_NEEDED_RE.search(f"{output or ''} {note or ''}"))
    what = (f"merged into #{merged_into}" if merged_into else "dropped" if status == "dropped" else
            "no longer needed" if gone else "done")
    open_ = f"status IN {OPEN_STATES} AND kind='work'" + (" AND created_at<=?" if before else "")
    at = (before,) if before else ()
    found = {}

    def add(row_id, why):
        if row_id not in (cid, merged_into) and row_id not in found:
            found[row_id] = why

    if gone:
        for r in conn.execute(f"SELECT id FROM items WHERE id IN (SELECT item_id FROM deps WHERE blocked_by=? "
                              f"AND kind<>'conflicts') AND {open_} ORDER BY id", (cid, *at)):
            add(r["id"], f"waits on #{cid}, which is {what}")
        for r in conn.execute(f"SELECT id FROM items WHERE found_during=? AND {open_} ORDER BY id", (cid, *at)):
            add(r["id"], f"found during #{cid}, which is {what}")
    elif status == "done":
        # A conflicts link (two items that touch the same files) is no wait: such an item stays needed (#4849).
        for r in conn.execute(f"SELECT id FROM items WHERE id IN (SELECT blocked_by FROM deps WHERE item_id=? "
                              f"AND kind<>'conflicts') AND {open_} ORDER BY id", (cid, *at)):
            add(r["id"], f"#{cid} is done although it waited on this")
    if _REVERT_RE.search(f"{it['title']} {output or ''}"):
        for n in sorted({int(x) for x in _REF_RE.findall(f"{it['title']} {output or ''}")} - {cid}):
            for r in conn.execute(f"SELECT id FROM items WHERE found_during=? AND doer='human' AND {open_} "
                                  f"ORDER BY id", (n, *at)):
                add(r["id"], f"a person's item of #{n}, which #{cid} reverts")
    path = conn.execute("SELECT path FROM projects WHERE id=?", (it["project_id"],)).fetchone()["path"]
    rows = _text_rows(conn, f"i.kind='work' AND i.status IN {OPEN_STATES} AND i.backlog_at IS NULL AND i.id<>? "
                            "AND (i.project_id=? OR (p.path IS NOT NULL AND p.path=?))" + (
                                " AND i.created_at<=?" if before else ""), (cid, it["project_id"], path, *at))
    next_steps = {r[0] for r in conn.execute("SELECT item_id FROM deps WHERE blocked_by=? AND kind<>'conflicts' "
                                             "UNION SELECT id FROM items WHERE found_during=?", (cid, cid))} if not gone else set()
    words_min = setting(conn, "similar_min", project_id=it["project_id"])
    kept = {r[0] for r in conn.execute("SELECT DISTINCT item_id FROM events WHERE change LIKE 'kept after the close of #%'")}
    added = {r["item_id"]: (r["actor"], parse_iso(r["at"])) for r in conn.execute(
        "SELECT item_id, actor, at FROM events WHERE change LIKE 'added to %' AND (item_id IN "
        f"(SELECT id FROM items WHERE {open_} AND project_id=?) OR item_id=?)", (*at, it["project_id"], cid))}
    mine = added.get(cid)
    series = {i for i, (who, when) in added.items() if mine and who and who == mine[0]
              and abs(when - mine[1]) < SIMILAR_BATCH}
    common = _ident_counts(rows)
    says = _idents(said) - {f"#{cid}"}
    hexes = {x for x in says if _HEX_RE.fullmatch(x)}
    strong = _places(says) - hexes
    weak = (says - strong) | {x for x in _idents(it["context"]) if not x.startswith("#")}  # "after #12 ships"
    idf = _idf(conn)
    t = _words(it["title"])
    q = _vector(t + t + sorted(set(_words(it["context"]) + _words(output) + _words(note))), idf)
    near = []
    for d in rows:
        if d["id"] in found or d["id"] in next_steps or d["id"] == merged_into or d["id"] in series \
                or _title_series(it["title"], d["title"]):
            continue
        s = _cosine(q, _vector(d["key"], idf))
        uuids = {x for x in d["idents"] & hexes if f"uuid:{x}" in d["idents"] | says}  # not a commit
        named = sorted(x for x in d["idents"] & strong | uuids if common[x] <= IDENT_COMMON)
        hint = sorted(x for x in d["idents"] & weak - strong if common[x] <= IDENT_COMMON)
        score = s + (IDENT_BONUS if named else IDENT_BONUS / 2 if hint else 0)
        if named or hint:
            hit = score >= CLOSE_NEAR
        else:
            hit = d["id"] not in kept and words_min != "off" and s >= float(words_min)
        if hit:
            why = (f"names {named[0]}, as #{cid} does" if named else f"names {hint[0]}, as #{cid} does"
                   if hint else f"like #{cid}") + f" (score {min(score, 1):.2f})"
            near.append((d["doer"] != "human", -score, d["id"], why))
    for _, _, i, why in sorted(near)[:CLOSE_LIMIT]:
        add(i, why)
    t_ = iso(now())
    for i, why in found.items():
        conn.execute("INSERT OR IGNORE INTO close_checks(closed_id, item_id, why, found_at) VALUES (?,?,?,?)",
                     (cid, i, why, t_))
    return _close_rows(conn, "c.closed_id=? AND c.decided_at IS NULL", (cid,))


def _title_series(a, b):
    """Two titles of one series: the same words before the colon, another subject after it (#3291)."""
    if ":" not in a or ":" not in b:
        return False
    (x, rx), (y, ry) = (t.split(":", 1) for t in (a, b))
    return _words(x) == _words(y) and bool(_words(x)) and _words(rx) != _words(ry)


def _close_rows(conn, where, params=()):
    """Rows of close_checks whose item is still open, with the item and the closed item."""
    return [dict(r) for r in conn.execute(
        "SELECT c.item_id id, i.title, i.doer, i.status, i.assignee, p.name project, c.why, c.ready, c.closed_id, "
        "x.title closed_title, x.status closed_status, c.found_at FROM close_checks c JOIN items i ON i.id=c.item_id "
        "JOIN projects p ON p.id=i.project_id JOIN items x ON x.id=c.closed_id "
        f"WHERE i.status IN {OPEN_STATES} AND " + where + " ORDER BY (i.doer<>'human'), c.found_at, c.rowid", params)]


def close_keep(conn, closed_id, item_id=None, note=None, actor=None):
    """Keep an item that a close check listed: it is still needed after the close of closed_id. The keep covers
    every close check that lists the item now, and a later close lists it again only for a link or an
    identifier (#3291). Without item_id, every item the check of closed_id still lists."""
    with tx(conn):
        _item(conn, closed_id)
        rows = _close_rows(conn, "c.closed_id=? AND c.decided_at IS NULL" + (" AND c.item_id=?" if item_id else ""),
                           (int(closed_id), int(item_id)) if item_id else (int(closed_id),))
        if not rows:
            raise RiverError(f"the close check of #{closed_id} lists " + (f"#{item_id} not (or it was decided)"
                             if item_id else "nothing that is open and undecided")
                             + "; what it lists: maxpm cleanup")
        why = (note or "").strip()
        for r in rows:
            conn.execute("UPDATE close_checks SET decided_by=?, decided_at=?, note=? WHERE item_id=? "
                         "AND decided_at IS NULL", (actor, iso(now()), why or None, r["id"]))
            _event(conn, r["id"], actor, f"kept after the close of #{closed_id}" + (f": {why}" if why else ""))
    return {"closed": int(closed_id), "kept": [{"id": r["id"], "title": r["title"]} for r in rows]}


def close_open(conn, project=None):
    """The items that a close may have made unnecessary and nobody decided (kept, closed, or merged), person
    items first: for maxpm cleanup and the manager."""
    names = _names(conn, project) if project else None
    out = {}
    for r in _close_rows(conn, "c.decided_at IS NULL AND i.backlog_at IS NULL"):
        if names is not None and r["project"] not in names:
            continue
        e = out.setdefault(r["id"], {k: r[k] for k in ("id", "title", "doer", "status", "assignee", "project")}
                           | {"closes": []})
        e["closes"].append({"id": r["closed_id"], "title": r["closed_title"], "status": r["closed_status"],
                            "why": r["why"]})
    return sorted(out.values(), key=lambda e: (e["doer"] != "human", e["closes"][0]["id"], e["id"]))


def close_scan(conn, since, project=None, actor=None):
    """Run the close check on the work items closed since that time that it has not checked (closes before
    #3286, or after a reopen). Each check leaves out the items made after its close."""
    names = _names(conn, project) if project else None
    t = _since(conn, since)
    n = 0
    for r in conn.execute(f"SELECT i.id, p.name FROM items i JOIN projects p ON p.id=i.project_id WHERE i.kind='work' "
                          f"AND i.status IN {CLOSED_STATES} AND i.closed_at>=? AND NOT EXISTS (SELECT 1 FROM "
                          f"close_checks c WHERE c.closed_id=i.id) ORDER BY i.closed_at", (t,)).fetchall():
        if names is not None and r["name"] not in names:
            continue
        with tx(conn):
            it = _item(conn, r["id"])
            m = re.match(r"duplicate of #(\d+)", it["output"] or "")
            _close_check(conn, it, it["status"], merged_into=int(m.group(1)) if m else None, before=it["closed_at"])
        n += 1
    return n


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
    if res["kind"] in ("review", "deploy"):
        what = (f"review #{res['id']} of {res.get('target') or 'a release'} passed: its deploy can start"
                if res["kind"] == "review" else f"deploy #{res['id']} to {res.get('target') or 'its target'} is done")
        with tx(conn):
            _wake_manager(conn, actor, f"{what} ({(res.get('output') or '')[:160]})", res["id"])
    return res


def _wake_manager(conn, actor, text, item_id=None):
    """Inside a tx: a notice to the active manager that wakes its watch at once (level now, which only river
    sends): a ship request, a review that passed, a deploy that is done (#4289). Before, the manager heard of
    them at its next wake, and a release waited on it (#4278 passed at 22:16Z, the manager saw it at 22:37Z).
    Nothing when no manager is active or when the manager itself did it."""
    manager = active_manager(conn)
    if not manager or manager == actor:
        return None
    return _send(conn, "notice", actor or "maxpm", text, to=manager, item_id=item_id, level=WAKE_LEVEL)


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
                # A target owner that is the manager gets it at once (#4289), as every manager does below.
                _send(conn, "notice", actor or "maxpm", f"ship request: #{it['id']} {it['title']} joins deploy "
                      f"#{dep['id']} for {tg['name']}", to=tg["owner"], item_id=dep["id"],
                      level=WAKE_LEVEL if tg["owner"] == active_manager(conn) else "low")
            if not tg["owner"] or tg["owner"] != active_manager(conn):
                _wake_manager(conn, actor, f"ship request: #{it['id']} {it['title']} joins deploy #{dep['id']} for "
                              f"{tg['name']}", dep["id"])
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
        why = reviewer_refusal(conn, a, actor)  # an author, or a used session: serve starts another reviewer
        if why:
            if brief is not None:
                brief.setdefault("review_refused", why)
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


UNNEEDED = "a close may have made it unnecessary"  # the cleanup reason of the close check (#3286)


def cleanup(conn, project=None, git=True, closes=None, actor=None):
    """Open items that may be done or stale although the queue says otherwise, with the evidence.

    Suspect: a lease ran out without done; a commit in the project folder names the item (#id);
    a person's item whose touched files changed after it was made; ready and never claimed for
    stale_after; a needs-you notice whose title is out of date. A check (maxpm check) after the
    evidence clears the item. First, person items first: the items that a close may have made unnecessary
    and nobody decided (close_open); closes (a time back, 7d) first runs the close check on the closes of that
    time that it has not checked."""
    if closes:
        close_scan(conn, closes, project, actor)
    ann = annotate(conn)
    t = now()
    project = _project(conn, project)["name"] if project is not None else None
    items = [a for a in ann.values() if a["status"] == "open" and not a["project_archived"] and not a["backlog"]
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
    first = close_open(conn, project)
    for e in first:
        for c in e["closes"]:
            flag(ann[e["id"]], UNNEEDED, f"#{c['id']} {c['title'][:60]} ({c['status']}): {c['why']}")
        out[e["id"]]["closes"] = [c["id"] for c in e["closes"]]
    order = {e["id"]: n for n, e in enumerate(first)}
    return sorted(out.values(), key=lambda e: (order.get(e["id"], len(order)), ann[e["id"]]["sort_key"]))


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


def takeovers_seen(conn, ids, actor=None):
    """OK all on the You view: mark every listed takeover seen in one transaction. The page sends the ids it
    shows, so a takeover that came after the page drew the list stays on it. One request, not one per item:
    811 requests in a row took minutes (#5365). Ids that are not a takeover are skipped."""
    ids = sorted({int(i) for i in ids or []})
    with tx(conn):
        n = 0
        for k in range(0, len(ids), 500):  # SQLite's limit of bound parameters
            part = ids[k:k + 500]
            n += conn.execute(f"UPDATE items SET takeover_seen=1 WHERE takeover_by IS NOT NULL AND takeover_seen=0 "
                              f"AND id IN ({','.join('?' * len(part))})", part).rowcount
    return {"seen": n}


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
        conn.execute("DELETE FROM close_checks WHERE closed_id=?", (it["id"],))  # the next close checks again
        _event(conn, it["id"], actor, "reopened")
    return item_show(conn, item_id)


# ---------------------------------------------------------------- reads

# ---------------------------------------------------------------- token usage (#1102)

# Anthropic bills a cache read at 0.1 of the input price, a 1-hour cache write at 2.0 and a 5-minute write at
# 1.25. "eq" is input-token equivalents on that scale; output tokens are shown apart.
EQ_READ, EQ_WRITE_1H, EQ_WRITE_5M = 0.1, 2.0, 1.25
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def parse_prices(text):
    """model_prices: {family: (input, cache read, 5-minute write, 1-hour write)} in dollars per million tokens, from
    'opus=4/0.2/5/8, sonnet=2/0.1/2.5/4'. With fewer prices: a read at 0.1 of the input price, and writes at the
    input price (gpt=1.25), or at the one write price given."""
    out = {}
    for part in re.split(r"[,\s]+", (text or "").strip()):
        if not part:
            continue
        m = re.match(r"^([\w.-]+)=([0-9.]+(?:/[0-9.]+){0,3})$", part)
        if not m:
            raise RiverError(f"model_prices: {part!r} is not <family>=<input>/<cache read>/<5m write>/<1h write> "
                             f"(dollars per million tokens), for example opus=4/0.2/5/8")
        nums = [float(x) for x in m.group(2).split("/")]
        if len(nums) == 1:  # no cache prices given: a read at 0.1 of the input price, a write at the input price
            nums += [nums[0] * 0.1, nums[0], nums[0]]
        elif len(nums) == 2:
            nums += [nums[0], nums[0]]
        elif len(nums) == 3:  # one write price for both lifetimes
            nums += [nums[2]]
        out[m.group(1).lower()] = tuple(nums)
    return out


def model_price(conn, model):
    """(input, cache read, 5m write, 1h write) in dollars per million tokens for a model name or id (claude-opus-5-5,
    opus): the model_prices family whose name is in it, the longest first; None for a model of no family there."""
    if not model:
        return None
    prices = parse_prices(setting(conn, "model_prices"))
    m = model.lower()
    for fam in sorted(prices, key=len, reverse=True):
        if fam in m:
            return prices[fam]
    return None


def claude_session_from_env(env=None):
    """The Claude Code session id this command runs in (its transcript is <id>.jsonl), or None."""
    sid = (env if env is not None else os.environ).get("CLAUDE_CODE_SESSION_ID", "").strip()
    return sid if _SESSION_ID_RE.match(sid) else None


def session_from_env(env=None):
    """The session id this command runs in, for agent_sessions (#5212): from the first agent adapter whose IDENTITY
    takes the id from an environment variable that is set (source 'env') and whose chat log is found by that id
    (CHAT['ref'] 'session'), when the id matches its IDENTITY['id_pattern'] (else agents.SAFE_ID); or None. Claude
    Code: CLAUDE_CODE_SESSION_ID; Hermes: HERMES_SESSION_ID. Codex's thread id goes to native_address instead (its
    CHAT['ref'] is 'address'). The order of agents.from_env: every other agent first, then codex, then claude: an
    agent started from a Claude Code shell keeps CLAUDE_CODE_SESSION_ID."""
    from . import agents
    env = os.environ if env is None else env
    got = agents.load()
    for m in agents.env_order():
        ad, ident = got[m], got[m].identity or {}
        if ident.get("source") != "env" or not ident.get("env") or ad.chat_ref() != "session":
            continue
        sid = str(env.get(ident["env"]) or "").strip()
        if sid and re.fullmatch(ident.get("id_pattern") or agents.SAFE_ID, sid):
            return sid
    return None


# ---------------------------------------------------------------- activity hooks

# Claude Code runs the hooks of its adapter (river/agents/claude.py) in a session that river started (launch option
# claude_activity_hooks): each one runs `maxpm report --hook claude`, which reads the hook's JSON on stdin; the
# adapter's read_hook says what it means (a hook report), and hook_report records it (#4288, #5183).
ACTIVITY_NOTE = "stands still"  # the alerts of the hooks to the manager start with "<agent> stands still"


def report_command():
    """The maxpm report command that an agent's hooks run (argv words): the maxpm on PATH when there is one, so the
    hook finds the same queue as the session's own commands."""
    import shutil
    return [shutil.which("maxpm") or "maxpm", "report"]


def activity_hook_settings():
    """The Claude Code settings (claude --settings) with river's hooks (the claude adapter's hook_settings)."""
    from . import agents
    return agents.get("claude").module.hook_settings(report_command())


def hook_file(adapter):
    """The hook file of an adapter with the hooks_file capability, beside the queue: its install_hooks writes it when
    its text changes (Claude Code: claude-hooks.json, which claude --settings reads)."""
    return adapter.install_hooks(None, report_command())[-1]


def activity_hook_file():
    """Claude Code's hook file (hook_file of the claude adapter)."""
    from . import agents
    return hook_file(agents.get("claude"))


def activity_agent(conn, sid=None, env=None):
    """The agent of a hook: MAXPM_AGENT of the hook's environment when that agent is registered; else the one that
    ran a maxpm command in Claude Code session sid (agent_sessions, the newest); else None. The environment comes
    first: the manager's command for another agent (--as) put that agent into the map of the manager's session,
    and every hook of the manager then counted as that agent (#5060)."""
    env = env if env is not None else os.environ
    name = (env.get("MAXPM_AGENT") or "").strip()
    if name and conn.execute("SELECT 1 FROM agents WHERE name=? AND kind='ai'", (name,)).fetchone():
        return name
    if sid:
        r = conn.execute("SELECT s.agent FROM agent_sessions s JOIN agents a ON a.name=s.agent AND a.kind='ai' "
                         "WHERE s.session_id=? ORDER BY s.first_seen DESC LIMIT 1", (sid,)).fetchone()
        if r:
            return r["agent"]
    return None


def _activity_call(prompt):
    """The tool call of a PermissionRequest, in a few words: 'Bash: git push origin main'."""
    try:
        p = json.loads(prompt or "")
    except ValueError:
        return None
    if not isinstance(p, dict) or not p.get("tool"):
        return None
    inp = p.get("input") or {}
    what = next((inp[k] for k in ("command", "file_path", "url", "path", "pattern") if isinstance(inp.get(k), str)), "")
    what = " ".join(what.split())
    return f"{p['tool']}: {what[:200]}" if what else p["tool"]


def _activity_alert(conn, agent, item, text):
    """Alert the active manager about an agent that stands still, inside a tx; blocked, so its watch wakes at
    once. Nothing when no manager is active, when the agent is that manager, or when the same alert is unread."""
    manager = active_manager(conn)
    if not manager or manager == agent:
        return None
    body = f"{agent} {ACTIVITY_NOTE}: {text}"
    if conn.execute("SELECT 1 FROM messages WHERE kind='alert' AND from_agent=? AND to_agent=? AND body=? "
                    "AND read_at IS NULL", (agent, manager, body)).fetchone():
        return None
    return _send(conn, "alert", agent, body, to=manager, item_id=item, blocked="waits")


def _talk(prompt):
    """True for a prompt that someone typed or sent: not the harness's own wake for a background task that ended
    (a task notification), and not a message that MaximizePM delivered ([maxpm ...]; maxpm wait wakes on those)."""
    text = prompt.strip() if isinstance(prompt, str) else ""
    return bool(text) and not text.startswith(("<task-notification", "[maxpm "))


def _activity_item(conn, agent):
    return conn.execute("SELECT id, title FROM items WHERE assignee=? AND status='in_progress' ORDER BY id",
                        (agent,)).fetchone()


def activity_event(conn, payload, env=None):
    """Record one Claude Code hook event (its JSON input): hook_report with the claude adapter."""
    return hook_report(conn, "claude", payload, env)


def hook_report(conn, agent_id, payload, env=None):
    """maxpm report --hook <agent>: an agent's hook passes its own input on stdin, and the adapter's read_hook says
    what it means (a hook report: river/agents HOOK_REPORT_KEYS). Stored as maxpm report stores a state:
    activity_at, activity_event 'report:<state>' (a report with no state keeps only the time), and for a blocked
    call the tool call that waits (activity_prompt; working clears it). A permission prompt that shows, or a turn
    that ends, while the agent holds an item alerts the active manager at once (_activity_alert): not a turn end
    while the agent waits on a person's answer (maxpm ask), has a stop request, or waits on its own background
    work (the report's waits: a background command or subagent, a wakeup; recorded in activity_waits until its
    next working report, #4293). That work wakes the session, and its next turn end is judged again. A prompt that
    a person typed (working with a prompt) closes the questions it answers and moves the clock of maxpm wait.
    Returns {"agent", "event", "state", "prompt": bool, "alert": message id or None, "answered", "remind"}."""
    from . import agents
    env = env if env is not None else os.environ
    rep = agents.get(agent_id).read_hook(payload)
    agent = activity_agent(conn, rep and rep["session_id"], env)
    if rep and rep["run"] is not None:
        # A hook before a shell command: a check only, nothing recorded (it runs before every Bash call).
        return {"agent": agent, "event": rep["event"], "state": None, "prompt": False, "alert": None, "answered": [],
                "remind": None, "deny": push_guard(conn, agent, rep["run"]) if agent else None}
    if not agent or not rep:
        return {"agent": agent, "event": rep["event"] if rep else str(payload.get("hook_event_name") or ""),
                "state": None, "prompt": False, "alert": None, "answered": [], "remind": None}
    record_session_url(conn, agent, session_url_from_env(env))
    state, typed = rep["state"], rep["prompt"]
    t, alert, answered, remind = iso(now()), None, [], None
    with tx(conn):
        a = conn.execute("SELECT activity_prompt, activity_waits, activity_event, stop_at, role FROM agents WHERE name=?",
                         (agent,)).fetchone()
        prompt, waits = a["activity_prompt"], a["activity_waits"]
        if state == "blocked" and rep["call"]:
            prompt = json.dumps(rep["call"])
        elif state == "working":
            prompt = waits = None
        if typed is not None and _talk(typed):
            conn.execute("UPDATE agents SET talked_at=? WHERE name=? AND role='expert'", (t, agent))
            if not typed.strip().startswith("<"):  # tags: a message of another session, not a person
                answered = answered_in_chat(conn, agent, typed.strip(), t)
            # A person (or another session) talks with a session that waits for work: wait_max counts from the
            # talk, so maxpm wait does not end the session during it or soon after (#4948).
            conn.execute("UPDATE agents SET waiting_since=? WHERE name=? AND waiting_since IS NOT NULL", (t, agent))
        if state == "idle":
            found = rep["waits"]
            if found is None:  # the input does not say: a process below the session, not this hook (busy_now)
                found = ["a process"] if agent in busy_now(conn, [agent], skip={os.getpid(), os.getppid()}) else []
            waits = json.dumps(found) if found else None
        conn.execute("UPDATE agents SET activity_at=?, activity_event=?, activity_prompt=?, activity_waits=? WHERE name=?",
                     (t, f"report:{state}" if state else a["activity_event"], prompt, waits, agent))
        # An expert sits at its prompt with the person: its turn ends and prompts are no news for the manager.
        held = _activity_item(conn, agent) if a["role"] != "expert" else None
        if held and state == "blocked" and rep["shows"]:
            call = _activity_call(prompt)
            alert = _activity_alert(conn, agent, held["id"],
                                f"it waits on a permission prompt in its terminal on #{held['id']} {held['title'][:80]}"
                                + (f"; it asks to run {call}" if call else "")
                                + f" ({(rep['message'] or '')[:160]}). Answer it in its terminal or "
                                  f"its session, or tell the person.")
        elif held and state == "idle" and not waits and not a["stop_at"] and agent not in waits_on_person(conn, [agent]):
            alert = _activity_alert(conn, agent, held["id"],
                                f"it ended its turn while it holds #{held['id']} {held['title'][:80]}: it sits at its "
                                f"prompt and runs no maxpm wait. Look at its terminal or session.")
        if alert:
            conn.execute("UPDATE agents SET activity_told=? WHERE name=?", (t, agent))
        if state == "idle" and not rep["again"]:
            # The asker closes its own questions (#5001): once for each question, at a turn end after it.
            due = [q for q in asked_open(conn, agent) if not q["asker_reminded"]
                   and now() - parse_iso(q["created_at"]) >= ASKER_REMIND_AFTER]
            if due:
                conn.execute(f"UPDATE messages SET asker_reminded=? WHERE id IN ({','.join('?' * len(due))})",
                             (t, *[q["id"] for q in due]))
                remind = "MaximizePM: " + asked_line(agent, due) + ". If it still waits for the person, end your turn again."
    if answered:
        sync_needs_you(conn)  # the person's 'waits for you' of those questions closes now
    return {"agent": agent, "event": rep["event"], "state": state, "prompt": typed is not None, "alert": alert,
            "answered": answered, "remind": remind}


def push_guard(conn, agent, command):
    """Why MaximizePM refuses this shell command of the agent now, or None (#5381). A command that matches the setting
    push_guard (a push, gh pr merge, a launch) waits while the agent has an unread alert (it may say 'stop', 'do not
    push', or 'a prerequisite came first'), or holds an item that someone blocked. The text rule 'read the inbox
    before a push' failed ten times; a refusal that names the alert does not."""
    pat = setting(conn, "push_guard", agent=agent)
    if not pat or not command or not re.search(pat, command):
        return None
    alerts = conn.execute(f"SELECT m.id, m.from_agent, m.body FROM messages m WHERE {_TO_ME} AND m.kind='alert' "
                          "AND m.read_at IS NULL AND m.from_agent<>? ORDER BY m.id", (agent, agent, agent)).fetchall()
    # A block that another agent or a person set: the agent's own block (it waits on an outside step) holds nothing.
    blocked = conn.execute("SELECT id, blocked_reason FROM items WHERE assignee=? AND status IN ('in_progress','held') "
                           "AND blocked_reason IS NOT NULL AND COALESCE(blocked_set_by, '')<>? ORDER BY id",
                           (agent, agent)).fetchall()
    if not alerts and not blocked:
        return None
    why = [f"alert #{m['id']} from {m['from_agent']}: {' '.join(m['body'].split())[:160]}" for m in alerts[:3]]
    why += [f"#{b['id']} is blocked: {' '.join(b['blocked_reason'].split())[:120]}" for b in blocked[:2]]
    more = len(alerts) + len(blocked) - len(why)
    return ("MaximizePM holds this command (push_guard): " + "; ".join(why) + (f"; and {more} more" if more > 0 else "")
            + f". Read it first: maxpm --as {agent} inbox"
            + (f" (and maxpm --as {agent} show {blocked[0]['id']})" if blocked else "")
            + ". Then do what it says, and run the command again only when it still applies.")


def state_adapter(conn, agent):
    """The adapter of the agent CLI that `agent` runs in (agents.agent_type: claude-code, codex), or None when it is
    not known (a custom command, or no command of it ran yet)."""
    from . import agents
    r = conn.execute("SELECT agent_type FROM agents WHERE name=?", (agent,)).fetchone()
    return agents.find(r["agent_type"]) if r and r["agent_type"] else None


def _state_source(conn, agent):
    """Where the agent's state comes from: its adapter's STATE['state'] ('hook' or 'detect'), or None."""
    a = state_adapter(conn, agent)
    return (a.state or {}).get("state") if a else None


def activity_watch(conn):
    """One look of maxpm serve (each pass): an agent with hooks that holds an item and reported no hook event
    and ran no maxpm command for activity_silence alerts the active manager, once for each silence. Not an agent
    in maxpm wait, one that waits on a person's answer, one with a stop request, or one that runs a background
    command: one whose last turn ended while its own background work runs (activity_waits, for at most busy_max),
    or one with a process below it (busy_now). Returns the agents it alerted about."""
    limit = parse_duration(setting(conn, "activity_silence"))
    if not limit:
        return []
    t, told = now(), []
    bg = parse_duration(setting(conn, "busy_max"))
    rows = [r for r in conn.execute("SELECT name, activity_at, activity_event, activity_prompt, activity_told, activity_waits, "
                                    "last_seen FROM agents WHERE kind='ai' AND activity_at IS NOT NULL AND stop_at IS NULL "
                                    "AND waiting_since IS NULL AND COALESCE(role, '') NOT IN ('expert', 'assistant')").fetchall()
            if r["activity_told"] != r["activity_at"]
            and t - max(parse_iso(r["activity_at"]), parse_iso(r["last_seen"])) >= limit
            and not (r["activity_waits"] and t - parse_iso(r["activity_at"]) < bg)]
    # The silence of an agent whose state its pane gives (STATE 'detect'): the pane watcher reports a change of
    # state only, so a long turn is no silence; this watch is for the hooks.
    rows = [r for r in rows if _state_source(conn, r["name"]) != "detect"]
    if not rows:
        return []
    waiting = waits_on_person(conn, [r["name"] for r in rows])
    busy = busy_now(conn, [r["name"] for r in rows])
    with tx(conn):
        for r in rows:
            held = _activity_item(conn, r["name"])
            if not held or r["name"] in waiting or r["name"] in busy:
                continue
            quiet = _short(t - max(parse_iso(r["activity_at"]), parse_iso(r["last_seen"])))
            call = _activity_call(r["activity_prompt"])
            last = (f"it waits on a permission prompt for {call}" if call
                    else f"its last event: {r['activity_event']} at {show_time(r['activity_at'])}")
            if _activity_alert(conn, r["name"], held["id"], f"nothing for {quiet} while it holds #{held['id']} "
                           f"{held['title'][:80]} ({last}). Look at its terminal or session."):
                told.append(r["name"])
            conn.execute("UPDATE agents SET activity_told=activity_at WHERE name=?", (r["name"],))
    return told


REPORT_STATES = ("working", "idle", "blocked")  # maxpm report --state (river/agents STATES)
REPORT_GONE = "report:gone"  # activity_event after maxpm release: the session ended


def report(conn, agent, state, message=None, session_id=None, seq=None, resume=None):
    """maxpm report (docs/agent-adapters.md, #5167): an agent's own hook, plugin or pane rule tells its state. It is
    stored where the page reads the hooks' state now: activity_at, activity_event 'report:<state>', and for blocked
    activity_prompt (a JSON {"message"}), so the Sessions tab shows a prompt; working and idle clear it. --session-id
    records the agent's session id (agent_sessions), and the words after -- the command that resumes it
    (agents.resume_argv, JSON). --seq: a report whose seq is not above the last one of the same session is late and
    changes nothing (a newer report already holds). Returns {"agent", "state", "stored": bool, "seq"}."""
    if state not in REPORT_STATES:
        raise RiverError(f"maxpm report --state is one of {', '.join(REPORT_STATES)}, not {state!r}")
    if not agent:
        raise RiverError("maxpm report needs the agent: maxpm --as <agent> report ... (or MAXPM_AGENT)")
    if seq is not None and seq < 0:
        raise RiverError(f"maxpm report --seq is a whole number from 0, not {seq}")
    t = iso(now())
    with tx(conn):
        a = conn.execute("SELECT kind, report_seq, report_session FROM agents WHERE name=?", (agent,)).fetchone()
        if not a or a["kind"] != "ai":
            raise RiverError(f"{agent} is no registered agent; maxpm report is for an agent's session: "
                             f"maxpm register {agent} first")
        same = session_id is None or session_id == a["report_session"]
        if seq is not None and a["report_seq"] is not None and same and seq <= a["report_seq"]:
            return {"agent": agent, "state": state, "stored": False, "seq": a["report_seq"],
                    "why": f"seq {seq} is not above {a['report_seq']}, the last report of this session"}
        prompt = json.dumps({"message": (message or "")[:400]}) if state == "blocked" else None
        conn.execute("UPDATE agents SET activity_at=?, activity_event=?, activity_prompt=?, activity_waits=NULL, "
                     "report_seq=?, report_session=COALESCE(?, report_session) WHERE name=?",
                     (t, f"report:{state}", prompt, seq if seq is not None else (a["report_seq"] if same else None),
                      session_id, agent))
        if session_id:
            conn.execute("INSERT OR IGNORE INTO agent_sessions (agent, session_id, first_seen) VALUES (?,?,?)",
                         (agent, session_id, t))
        if resume:
            conn.execute("UPDATE agents SET resume_argv=? WHERE name=?", (json.dumps(list(resume)), agent))
    return {"agent": agent, "state": state, "stored": True, "seq": seq}


# A --seq at or above this is a time (Pi, OpenCode and Copilot send milliseconds, Grok nanoseconds), not a count.
SEQ_TIME_MIN = 10 ** 12


def clock_ns():
    """The time in nanoseconds: the release seq of an agent whose seq is a time (tests replace it)."""
    import time
    return time.time_ns()


def _release_seq(seq):
    """The last seq of an agent after maxpm release (#5231): when its seq is a time, the release's time in the same
    unit (milliseconds, microseconds or nanoseconds, by the size of the seq), so a report that its plugin started
    before the release and that reaches maxpm after it is late and changes nothing. A count stays as it is: its
    unit is no time."""
    if seq is None or seq < SEQ_TIME_MIN:
        return seq
    unit = 10 ** 6 if seq < 10 ** 15 else 10 ** 3 if seq < 10 ** 18 else 1
    return max(seq, clock_ns() // unit)


def report_release(conn, agent):
    """maxpm release with no item (docs/agent-adapters.md, #5167): the agent's session ended (its hook at the end of
    the session runs it). The agent is gone at once, as when its process ends (_agent_state); its next maxpm command
    or report makes it present again. Its items, goals and targets free as for any gone session.

    Hook plugins (OpenCode, Copilot) start each maxpm command and do not wait for it, so a report that they started
    before the release can reach maxpm after it (#5231). When the agent's --seq is a time, the release moves its last
    seq to the release's time: such a late report of the same session changes nothing (report), and a report that
    starts after the release, or one of another session, makes the agent present again."""
    if not agent:
        raise RiverError("maxpm release with no item ends the session of an agent: maxpm --as <agent> release; "
                         "to give an item back: maxpm release <id>")
    with tx(conn):
        a = conn.execute("SELECT kind, report_seq FROM agents WHERE name=?", (agent,)).fetchone()
        if not a or a["kind"] != "ai":
            raise RiverError(f"{agent} is no registered agent; to give an item back: maxpm release <id>")
        conn.execute("UPDATE agents SET activity_at=?, activity_event=?, activity_prompt=NULL, activity_waits=NULL, "
                     "report_seq=? WHERE name=?", (iso(now()), REPORT_GONE, _release_seq(a["report_seq"]), agent))
        _event(conn, None, agent, f"agent {agent} reported that its session ended (maxpm release)")
    return {"agent": agent, "state": "gone"}


def session_owner(conn, sid):
    """The agent that MaximizePM recorded last for session `sid` (it may be gone now), or None."""
    r = sid and conn.execute("SELECT agent FROM agent_sessions WHERE session_id=? ORDER BY first_seen DESC, rowid DESC "
                             "LIMIT 1", (sid,)).fetchone()
    return r["agent"] if r else None


def record_session(conn, name, sid):
    """Remember that agent `name` ran a command in session `sid` (once per pair): a Claude Code session, or the
    session of any agent whose adapter names its id (session_from_env, #5212)."""
    if not (name and sid) or conn.execute("SELECT 1 FROM agent_sessions WHERE agent=? AND session_id=?",
                                          (name, sid)).fetchone():
        return
    if conn.execute("SELECT 1 FROM agents WHERE name=? AND kind='ai'", (name,)).fetchone():
        with tx(conn):
            conn.execute("INSERT OR IGNORE INTO agent_sessions (agent, session_id, first_seen) VALUES (?,?,?)",
                         (name, sid, iso(now())))


record_claude_session = record_session  # its name before #5212


def transcripts_root():
    """Where Claude Code keeps session transcripts: <config dir>/projects/<folder>/<session id>.jsonl."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


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
    empty list with no transcript on this computer (another agent CLI) or with busy_max 0s, and for an agent whose
    adapter has no background commands (LAUNCH['background'] false: Codex)."""
    ad = state_adapter(conn, agent)
    if ad and not ad.launch.get("background"):
        return []
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


def _session_requests(sid, root=None):
    """(requests, fork) of a session id, from the chat log part of the first agent adapter that finds its log on
    this computer (chat_requests: each model call once, with its time and token counts; fork: it started as a fork),
    or None. root: the log folder that a test gives."""
    from . import agents
    for ad in agents.load().values():
        got = ad.chat_requests(sid, root)
        if got:
            return got
    return None


def usage_eq(u):
    """Input-token equivalents of a usage row (or of one request of chat_requests)."""
    return u["input"] + EQ_READ * u["cache_read"] + EQ_WRITE_1H * u["write_1h"] + EQ_WRITE_5M * u["write_5m"]


def measure_usage(conn, item_id, root=None):
    """Read the token usage of the sessions that held the item from their chat logs (the adapters' chat_requests), and
    store it (item_usage; a new measurement replaces the old one). A request is charged to the item when the
    session's agent held it at that time; when the agent held several items, to the one it took last.
    Returns the stored rows."""
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
                row["turns"] += 1
                for k in ("input", "cache_read", "write_1h", "write_5m", "output"):
                    row[k] += u[k]
                row["peak_context"] = max(row["peak_context"], u["input"] + u["cache_read"] + u["write_1h"] + u["write_5m"])
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
        got = _session_requests(session, root)
        reqs = sorted(got[0].values(), key=lambda r: r[0]) if got else []
        last = reqs[-1][1] if reqs else {}
        tokens = sum(last.get(k) or 0 for k in ("input", "cache_read", "cache_write")) or None
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
    ad = _adapters().find(platform) if platform else None
    if not (ad and ad.launch.get("fork")) or not item.get("goals") or item.get("no_goal_context"):
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
    label = lambda x: "ready" if ann[x]["ready"] else "backlog" if ann[x]["backlog"] else ann[x]["status"]
    a["waits_on_detail"] = [{"id": b, "title": ann[b]["title"], "short": ann[b].get("short"), "status": label(b),
                             "kind": "feeds" if b in a["fed_by"] else "blocks"} for b in a["waits_on"]]
    a["unblocks_detail"] = [{"id": d, "title": ann[d]["title"], "short": ann[d].get("short"), "status": label(d)} for d in a["unblocks"]]
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
        # The backlog has its own list (maxpm backlog); --all shows it here too.
        rows = [a for a in rows if a["status"] in OPEN_STATES and not a["backlog"]]
    rows.sort(key=lambda a: (a["status"] in CLOSED_STATES, a["backlog"], not a["ready"], a["sort_key"]))
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
            "id": i, "title": a["title"], "status": a["status"], "assignee": a["assignee"], "backlog": a["backlog"],
            "backlog_note": a["backlog_note"] if a["backlog"] else None,
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
        if to == "manager" and not conn.execute("SELECT 1 FROM agents WHERE name='manager'").fetchone():
            to = active_manager(conn)  # the word manager: the manager session that is active now (#4288)
            if to is None:
                raise RiverError("no manager session is active now (maxpm who); a session becomes the manager with "
                                 "maxpm manage, or send it to a person")
        if to is not None:
            _agent(conn, to)
        manager = _is_manager(conn, to)
        # What MaximizePM knows without the flag: the sender released or blocked the item just before.
        sign = "sender" if blocked else ("item" if manager and kind in ("alert", "question")
                                         and _item_stands(conn, actor, item) else None)
        mid = _send(conn, kind, actor, body.strip(), to=to, item_id=item, reply_to=reply_to,
                    level=level if level != DEFAULT_LEVEL else None, blocked=sign)
        held = _hold_question(conn, mid) if kind in ("question", "alert") else None
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
    if held:
        # The manager's watch wakes at once for it (blocked); a platform with native_message brings it into the session.
        st = deliver_native(conn, held["manager"], _native_text("alert", "maxpm", held["body"]))
        with tx(conn):
            conn.execute("UPDATE messages SET native_status=? WHERE id=?", (st, held["alert"]))
    res = message_show(conn, mid)
    if held:
        res["held_note"] = (f"held for the manager {held['manager']}: it answers you, or sends the question on to {to}; "
                            f"{to} gets it by itself after {held['hold']} (question_hold). Your wait does not change")
    if manager and not res["blocked"] and not _is_person(conn, actor):
        # For the sender: how long the manager's watch lets it wait (the command says how to shorten that).
        res["manager_wait"] = setting(conn, "manage_wait_" + res["level"], agent=to)
    return res


def _hold_question(conn, mid):
    """Inside a tx, at the send of question mid (#5307). A question from a session to a person goes to the active
    manager first: mark wrote 'these sorts of questions should not come to me or block waiting for me' after worker
    questions he had settled already opened 'Waits for you' on his page. The question is held for the manager
    (held_for) until question_hold ends: the person's needs-you, Waits for you and phone do not show it. The manager
    gets a blocked alert in the question's thread, so its watch wakes at once; it answers (maxpm answer) or sends the
    question on (maxpm forward). Not for a question from a person or from a manager, and not with no active manager.
    Returns {"manager", "alert", "body", "hold"}, or None when the person has the question at once."""
    q = _message(conn, mid)
    if not _is_person(conn, q["to_agent"]) or _is_person(conn, q["from_agent"]) or _is_manager(conn, q["from_agent"]):
        return None
    hold = setting(conn, "question_hold", agent=q["from_agent"])
    boss = active_manager(conn) if parse_duration(hold).total_seconds() > 0 else None
    if not boss:
        return None
    person, about = q["to_agent"], (f" about #{q['item_id']}" if q["item_id"] is not None else "")
    conn.execute("UPDATE messages SET held_for=?, held_until=? WHERE id=?",
                 (boss, iso(now() + parse_duration(hold)), mid))
    if q["kind"] == "alert":  # an alert has no answer: the manager acts on it, drops it, or sends it on (#5311)
        body = (f"{q['from_agent']} alerts {person} (alert #{mid}{about}): {' '.join(q['body'].split())[:600]}. "
                f"It comes to you first; {person} does not see it yet. Do what it asks when {person} is not needed, "
                f"then close it: maxpm forward {mid} --drop --note \"<what you did>\". Else send it on to {person}: "
                f"maxpm forward {mid} [--note \"<what you add>\"]. With neither within {hold} (question_hold), "
                f"{person} gets it.")
    else:
        body = (f"{q['from_agent']} asks {person} (question #{mid}{about}): {' '.join(q['body'].split())[:600]}. "
                f"It comes to you first; {person} does not see it yet. Answer it when {person} is not needed: "
                f"maxpm answer {mid} \"...\". Else send it on to {person}: maxpm forward {mid} [--note \"<what you add>\"]. "
                f"With neither within {hold} (question_hold), {person} gets it.")
    alert = _send(conn, "alert", "maxpm", body, to=boss, item_id=q["item_id"], reply_to=mid, blocked="sender")
    if q["item_id"] is not None:
        _event(conn, q["item_id"], "maxpm", f"{q['kind']} #{mid} to {person} held for the manager {boss} ({hold})")
    return {"manager": boss, "alert": alert, "body": body, "hold": hold}


def _unhold_question(conn, q, why=None):
    """Inside a tx. Question q is no longer held for the manager (#5307): the manager answered or forwarded it, its
    question_hold ended, or the question closed. The manager's alert about it is read, so no watch wakes for it.
    why: the event on the item when the person gets the question now."""
    if not q["held_for"]:
        return
    t = iso(now())
    conn.execute("UPDATE messages SET held_for=NULL, held_until=NULL WHERE id=?", (q["id"],))
    conn.execute("UPDATE messages SET read_at=COALESCE(read_at, ?), state=CASE WHEN state='open' THEN 'read' ELSE state END "
                 "WHERE reply_to=? AND from_agent='maxpm' AND kind='alert' AND to_agent=?", (t, q["id"], q["held_for"]))
    if why and q["item_id"] is not None:
        _event(conn, q["item_id"], "maxpm", f"question #{q['id']} goes to {q['to_agent']}: {why}")


def _release_held(conn):
    """Inside a tx (sync_needs_you). A held question goes to the person when its question_hold ended, or when the
    manager it waits for is no longer the active manager; a closed question is no longer held."""
    rows = conn.execute("SELECT * FROM messages WHERE held_for IS NOT NULL").fetchall()
    if not rows and not conn.execute("SELECT 1 FROM items WHERE held_for<>'' LIMIT 1").fetchone():
        return
    boss, t = active_manager(conn), now()
    for q in rows:
        if (q["kind"] == "question" and q["state"] != "open") or (q["kind"] == "alert" and q["read_at"]) \
                or q["kind"] not in ("question", "alert"):
            _unhold_question(conn, q)
        elif not q["held_until"] or parse_iso(q["held_until"]) <= t:
            _unhold_question(conn, q, f"the manager {q['held_for']} did not answer or forward it within question_hold")
        elif q["held_for"] != boss:
            _unhold_question(conn, q, f"the manager {q['held_for']} is no longer active")
    # Person items (#5311): the same ends.
    for it in conn.execute("SELECT * FROM items WHERE held_for IS NOT NULL AND held_for<>''").fetchall():
        why = (None if it["status"] not in OPEN_STATES or it["doer"] != "human" else
               f"the manager {it['held_for']} did not do or forward it within question_hold"
               if not it["held_until"] or parse_iso(it["held_until"]) <= t else
               f"the manager {it['held_for']} is no longer active" if it["held_for"] != boss else "")
        if why != "":
            _unhold_item(conn, it, why)


def forward(conn, msg_id=None, note=None, actor=None, item=None, drop=False):
    """maxpm forward <id> [--note] | forward --item <id>: the manager sends a question or alert that it held (#5307,
    #5311), or a person item that it held, on to the person, who then sees it (Waits for you, needs-you, phone).
    note: what the manager adds for the person, a note in the thread. drop: an alert that the manager dealt with
    itself is closed, and the person never sees it."""
    if not actor:
        raise RiverError("forward needs an agent name: set MAXPM_AGENT or pass --as <name>")
    if (msg_id is None) == (item is None):
        raise RiverError("name what to forward: a held message (maxpm forward <message-id>) or a held person item "
                         "(maxpm forward --item <item-id>)")
    if not (_is_manager(conn, actor) or _is_person(conn, actor)):
        raise RiverError(f"only the manager forwards what it holds for a person; the item or message waits for it")
    note = note.strip() if note and note.strip() else None
    with tx(conn):
        if item is not None:
            it = _item(conn, item)
            if not it["held_for"]:
                raise RiverError(f"#{item} is with the person already; nothing to forward")
            if drop:
                raise RiverError(f"close #{item} with: maxpm done {item} --note \"<what you did>\" (or drop)")
            _unhold_item(conn, it, f"forwarded by {actor}" + (f": {note}" if note else ""))
            if note:
                _event(conn, item, actor, f"note for the person: {note}")
            return item_show(conn, item)
        q = _message(conn, msg_id)
        if q["kind"] not in ("question", "alert") or (q["kind"] == "question" and q["state"] != "open"):
            raise RiverError(f"message {msg_id} is no open question or alert; see: maxpm thread {msg_id}")
        if not q["held_for"]:
            raise RiverError(f"{q['kind']} {msg_id} is with {q['to_agent'] or 'its holder'} already; nothing to forward")
        if drop:
            if q["kind"] != "alert":
                raise RiverError(f"answer question {msg_id} instead: maxpm answer {msg_id} \"...\"")
            _unhold_question(conn, q)
            conn.execute("UPDATE messages SET read_at=COALESCE(read_at, ?), state='read' WHERE id=?",
                         (iso(now()), msg_id))
            if q["item_id"] is not None:
                _event(conn, q["item_id"], actor, f"closed alert #{msg_id} to {q['to_agent']} (the manager dealt with it)"
                       + (f": {note}" if note else ""))
            return message_show(conn, msg_id)
        _unhold_question(conn, q, f"forwarded by {actor}" + (f": {note}" if note else ""))
        if note:
            _send(conn, "note", actor, note, to=q["to_agent"], item_id=q["item_id"], reply_to=q["id"])
    return message_show(conn, msg_id)


def _unhold_item(conn, it, why):
    """Inside a tx. Person item it goes to the person (#5311): the manager forwarded it, or its hold ended. The
    manager's alert about it is read. '' marks the item as looked at, so it is not held a second time."""
    conn.execute("UPDATE items SET held_for='', held_until=NULL WHERE id=?", (it["id"],))
    conn.execute("UPDATE messages SET read_at=COALESCE(read_at, ?), state=CASE WHEN state='open' THEN 'read' ELSE state END "
                 "WHERE item_id=? AND from_agent='maxpm' AND kind='alert' AND to_agent=? AND reply_to IS NULL",
                 (iso(now()), it["id"], it["held_for"]))
    if why:
        _event(conn, it["id"], "maxpm", f"goes to the person: {why}")


def _hold_items(conn, ann):
    """Inside a tx (sync_needs_you). A person item (doer human: a 'Look at', an approval, a decision) that is ready
    while a manager is active goes to the manager first (#5311), as a question does: it is held (items.held_for)
    for question_hold, out of the person's needs-you, Waits for you, phone and lists. The manager gets a blocked alert,
    so its watch wakes at once; it does the item (maxpm done|drop --note) or sends it on (maxpm forward --item).
    ann: annotate(); a held item gets held_for there too. Exceptions, marked '' so they are looked at once: an item
    that a person, the manager or maxpm added, and every item while no manager is active."""
    todo = [a for a in ann.values() if a["ready"] and a["doer"] == "human" and a["held_for"] is None
            and not a["project_archived"]]
    if not todo:
        return
    boss = active_manager(conn)
    for a in todo:
        hold = setting(conn, "question_hold", item_id=a["id"], project_id=_project(conn, a["project"])["id"])
        if not boss:
            continue  # looked at again when a manager is active
        by = conn.execute("SELECT actor FROM events WHERE item_id=? ORDER BY id LIMIT 1", (a["id"],)).fetchone()
        by = by["actor"] if by else None
        if (by and (by == "maxpm" or _is_person(conn, by) or _is_manager(conn, by))) or a["assignee"] \
                or parse_duration(hold).total_seconds() <= 0:
            conn.execute("UPDATE items SET held_for='' WHERE id=?", (a["id"],))
            a["held_for"] = ""
            continue
        until = iso(now() + parse_duration(hold))
        conn.execute("UPDATE items SET held_for=?, held_until=? WHERE id=?", (boss, until, a["id"]))
        a["held_for"] = boss
        body = (f"Person item #{a['id']} [{a['project']}] {a['title']} is ready; it waits on a person. It comes to you "
                f"first; the person does not see it yet. Do it when the person is not needed: maxpm done {a['id']} "
                f"--note \"<what you did>\" (or drop). Else send it on: maxpm forward --item {a['id']} "
                f"[--note \"<what you add>\"]. With neither within {hold} (question_hold), the person gets it.")
        _send(conn, "alert", "maxpm", body, to=boss, item_id=a["id"], blocked="sender")
        _event(conn, a["id"], "maxpm", f"held for the manager {boss} ({hold}): it does the item or forwards it")


def answer(conn, msg_id, body, actor=None, in_chat=False, self_answer=False):
    """Answer a question: the answer goes to the asker and the question closes. in_chat (#4995) or self_answer
    (#5001): the asker records the answer that it has from its own chat or from any other channel; the answer is in
    the name of the person asked, and is not delivered again (the asker has it)."""
    if in_chat or self_answer:
        if not actor:
            raise RiverError("answering needs an agent name: set MAXPM_AGENT or pass --as <name>")
        if not body or not body.strip():
            raise RiverError("say what the person answered in your chat: maxpm answer <id> --in-chat \"<their words>\"")
        with tx(conn):
            q = _message(conn, msg_id)
            if q["kind"] != "question" or q["state"] != "open":
                raise RiverError(f"message {msg_id} is no open question; see: maxpm thread {msg_id}")
            if q["from_agent"] != actor:
                raise RiverError(f"--self and --in-chat are for the asker ({q['from_agent']}), who has the answer; "
                                 f"answer it yourself without them")
            mid = _close_in_chat(conn, q, body.strip(), iso(now()), "in the chat" if in_chat else "by the asker")
        return message_show(conn, mid)
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
        # Before the question closes: the asker waits at its prompt for this answer (the guide says so), and runs
        # no maxpm command that would read it.
        waits = q["from_agent"] in waits_on_person(conn, [q["from_agent"]])
        # The manager answers a question to a person (#5307): in the thread it is the manager's answer for the person.
        for_person = answered_for(conn, q, actor)
        _unhold_question(conn, q)
        mid = _send(conn, "answer", actor, body.strip(), to=q["from_agent"], item_id=q["item_id"], reply_to=q["id"])
        conn.execute("UPDATE messages SET state='answered', closed_at=?, read_at=COALESCE(read_at, ?), "
                     "to_agent=COALESCE(to_agent, ?) WHERE id=?", (t, t, actor, q["id"]))
        if q["item_id"] is not None:
            _event(conn, q["item_id"], actor, f"answered question #{q['id']}"
                   + (f" for {for_person} (the manager)" if for_person and _is_manager(conn, actor)
                      else f" for {for_person}" if for_person else ""))
            # The asker ended (it filed its question and stopped): a fresh session takes the item, with the answer.
            if not conn.execute("SELECT 1 FROM agents WHERE name=?", (q["from_agent"],)).fetchone():
                _want_fresh(conn, q["item_id"], f"[question #{q['id']} from {q['from_agent']}] {q['body']}\n"
                            f"[answer from {actor}] {body.strip()}")
    if waits:
        _deliver_answer(conn, q, mid, actor, body.strip(), for_person)
    return message_show(conn, mid)


# native_status of an answer that maxpm serve types into the asker's tmux pane when its idle prompt shows
# (server.paste_answers, #5303); then 'typed into pane %<n>'.
ANSWER_TO_PANE = "waits for the asker's idle prompt (maxpm serve types it there)"


def answered_for(conn, q, sender):
    """The person that question q asked, when an agent (the manager) and not that person answers it (#5307); else None."""
    to = q["to_agent"]
    return to if to and to != sender and _is_person(conn, to) and not _is_person(conn, sender) else None


def answer_text(q, sender, body, for_person=None):
    """The answer as the asker reads it in its session (#5303): it starts with '[maxpm ', so the asker's prompt hook
    (_talk) does not take it for a person's answer to its other open questions. for_person: the manager answered a
    question to that person (#5307)."""
    item = f" (#{q['item_id']})" if q["item_id"] is not None else ""
    by = f"{sender} for {for_person}" if for_person else sender
    return (f"[maxpm answer from {by} to your question #{q['id']}{item}] {body}\n"
            f"Your question was: {' '.join(q['body'].split())[:300]}")


def _deliver_answer(conn, q, mid, sender, body, for_person=None):
    """An answer to a question whose asker waits at its prompt (waits_on_person) goes into its session, as an alert
    does (#5303): the asker ran maxpm ask and ended its turn, so no maxpm command of its own reads the answer. The
    manager answered markhalstrom-site-3a49's question with maxpm answer, and the session sat at its prompt for an
    hour until the manager typed the answer into its tmux pane by hand. Through the platform's native_message when
    the asker has one; else maxpm serve types it into the asker's tmux pane when its idle prompt shows
    (server.paste_answers). An asker in no pane gets it in a fresh session (fresh_sessions), as other news."""
    st = deliver_native(conn, q["from_agent"], answer_text(q, sender, body, for_person))
    how = "sent into its session" if st == "sent" else (
        ("" if st.startswith("no native channel") else f"native {st}; ") + "maxpm serve types it into its pane")
    if st != "sent":
        st = ANSWER_TO_PANE
    with tx(conn):
        conn.execute("UPDATE messages SET native_status=? WHERE id=?", (st, mid))
        if q["item_id"] is not None:
            _event(conn, q["item_id"], "maxpm", f"answer #{mid} to {q['from_agent']}, which waits at its prompt: {how}")


def _close_in_chat(conn, q, body, t, how="in the chat"):
    """Inside a tx: the person asked (q's to_agent) answered question q in the asker's own chat (#4995), or the asker
    has the answer from another channel (how 'by the asker', #5001). The question closes with that text as its
    answer, in the person's name; the answer is read at once and not delivered, since the asker has it. Returns the
    answer's id."""
    who = q["to_agent"] or "a person"
    mid = _send(conn, "answer", who, body[:4000], to=q["from_agent"], item_id=q["item_id"], reply_to=q["id"])
    conn.execute("UPDATE messages SET state='read', read_at=?, native_status=? WHERE id=?", (t, how, mid))
    conn.execute("UPDATE messages SET state='answered', closed_at=?, read_at=COALESCE(read_at, ?) WHERE id=?",
                 (t, t, q["id"]))
    if q["item_id"] is not None:
        _event(conn, q["item_id"], who if how == "in the chat" else q["from_agent"],
               f"answered question #{q['id']} in the chat of {q['from_agent']}" if how == "in the chat"
               else f"question #{q['id']} answered: {q['from_agent']} has the answer of {who}")
    return mid


def withdraw(conn, msg_id, note, actor=None):
    """The asker closes its open question without an answer: the work made it moot (#5001). The person's 'waits for
    you' of it closes; a note in its thread says why."""
    if not actor:
        raise RiverError("withdraw needs an agent name: set MAXPM_AGENT or pass --as <name>")
    if not note or not note.strip():
        raise RiverError("say why the question is moot: maxpm withdraw <id> --note \"<why>\"")
    with tx(conn):
        q = _message(conn, msg_id)
        if q["kind"] != "question" or q["state"] != "open":
            raise RiverError(f"message {msg_id} is no open question; see: maxpm thread {msg_id}")
        if q["from_agent"] != actor:
            raise RiverError(f"only the asker ({q['from_agent']}) withdraws question {msg_id}; to answer it: "
                             f"maxpm answer {msg_id} \"...\"")
        t = iso(now())
        mid = _send(conn, "note", actor, f"withdrawn: {note.strip()}", to=q["to_agent"], item_id=q["item_id"],
                    reply_to=q["id"])
        conn.execute("UPDATE messages SET state='read', read_at=? WHERE id=?", (t, mid))
        conn.execute("UPDATE messages SET state='read', closed_at=?, read_at=COALESCE(read_at, ?) WHERE id=?",
                     (t, t, q["id"]))
        if q["item_id"] is not None:
            _event(conn, q["item_id"], actor, f"withdrew question #{q['id']}: {note.strip()}")
    sync_needs_you(conn)
    return {"withdrawn": q["id"], "note": note.strip(), "to": q["to_agent"]}


def asked_open(conn, agent):
    """The open questions that the agent asked a person (maxpm ask), oldest first (#5001)."""
    return [dict(r) for r in conn.execute(
        "SELECT m.id, m.to_agent, m.body, m.item_id, m.created_at, m.asker_reminded FROM messages m JOIN agents p ON "
        "p.name=m.to_agent AND p.kind='human' WHERE m.kind='question' AND m.state='open' AND m.from_agent=? "
        "ORDER BY m.id", (agent,))] if agent else []


def asked_line(agent, qs):
    """The reminder to an asker with open questions (#5001): close each one that is answered or moot."""
    if not qs:
        return ""
    who = ", ".join(sorted({q["to_agent"] for q in qs}))
    ids = ", ".join(f"#{q['id']}" for q in qs)
    return (f"you have {len(qs)} open question(s) to {who} ({ids}): close each that is answered, from any channel: "
            f"maxpm --as {agent} answer <id> --self \"<the answer>\"; or that is moot: maxpm --as {agent} withdraw <id> "
            f"--note \"<why>\"")


# The Stop hook reminds an asker once for each question older than this (#5001): a turn that ends at once after
# the question waits for the answer, and is not reminded.
ASKER_REMIND_AFTER = timedelta(minutes=2)


def answered_in_chat(conn, agent, prompt, at=None):
    """Inside a tx: a person typed a prompt in the agent's own chat (its UserPromptSubmit hook), so each open question
    from the agent to a person, asked before it, is answered by it (#4995): the person read the question in the
    chat and answered there, not with maxpm answer. Returns the closed question ids."""
    t = at or iso(now())
    rows = conn.execute("SELECT m.* FROM messages m JOIN agents p ON p.name=m.to_agent AND p.kind='human' "
                        "WHERE m.kind='question' AND m.state='open' AND m.from_agent=? AND m.created_at<=? ORDER BY m.id",
                        (agent, t)).fetchall()
    for q in rows:
        _close_in_chat(conn, q, prompt, t)
    return [q["id"] for q in rows]


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
    if _is_person(conn, actor):
        where += " AND m.held_for IS NULL"  # what the manager holds for the person is not theirs yet (#5311)
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
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        # An answer from someone other than the person asked: the manager answered for the person (#5307).
        q = by_id.get(r["reply_to"]) if r["kind"] == "answer" else None
        if q and q["kind"] == "question" and q["to_agent"] and q["to_agent"] != r["from_agent"] \
                and _is_person(conn, q["to_agent"]):
            r["for_person"] = q["to_agent"]
    return {"thread_id": root, "messages": rows}


def item_messages(conn, item_id):
    _item(conn, item_id)
    return [_msg_dict(r) for r in conn.execute(
        "SELECT m.*, NULL item_title FROM messages m WHERE m.item_id=? ORDER BY m.id", (int(item_id),))]


# ---------------------------------------------------------------- canvases (#4648)
# A canvas is a page that a session makes and maxpm keeps: HTML, Markdown, or SVG, each version in full, in this
# database (private to this computer). The same operations serve every agent: the maxpm canvas command, the MCP
# tool canvas, and HTTP on maxpm serve (/api/action) all call these functions. A person comments on a part of the
# page or on the whole page; the comments stay drafts until the person sends them, and then one message carries
# them to the session that wrote the canvas (or to the holder of its item).

CANVAS_KINDS = ("html", "md", "svg")
CANVAS_EXT = {".html": "html", ".htm": "html", ".md": "md", ".markdown": "md", ".svg": "svg"}
_CANVAS_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


# The canvas kit (#4983): styles and a script that maxpm adds to an HTML canvas with <meta name="maxpm-kit">, so a
# session writes only the content of a visual page (task graphs, cards, side by side, bars, timelines, mockups,
# pictures with notes, decision cards). maxpm canvas kit prints the starter page with every part.
CANVAS_KIT_DIR = Path(__file__).resolve().parent / "static" / "canvaskit"
_KIT_META = re.compile(r"<meta\s[^>]*name\s*=\s*[\"']?maxpm-kit\b", re.I)
_KIT_IDS = re.compile(r"\bdata-items?\s*=\s*[\"']([^\"']*)[\"']", re.I)
_PICTURE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                  ".webp": "image/webp", ".svg": "image/svg+xml"}


def canvas_kit():
    """The kit's starter page (every part, with comments) and how to use it."""
    return {"starter": (CANVAS_KIT_DIR / "starter.html").read_text(encoding="utf-8"),
            "how": "Copy the starter to a file, keep the parts you need, fill them, then: maxpm canvas put <file> "
                   "--name <name>. maxpm adds the kit's styles and script when the page is shown (light and dark, "
                   "phone width). Put it again after each change; each put is a new version."}


def uses_kit(body):
    """Whether an HTML canvas asks for the kit (<meta name="maxpm-kit">)."""
    return bool(_KIT_META.search(body or ""))


def canvas_items(conn, body, limit=300):
    """The queue items that a kit canvas names (data-items="12 13", data-item="12"), with their state now and what
    each waits on among them: {id: {title, status, assignee, doer, blocked, backlog, waits, deps}}."""
    ids = []
    for m in _KIT_IDS.finditer(body or ""):
        for x in re.split(r"[\s,]+", m.group(1)):
            x = x.lstrip("#")
            if x.isdigit() and int(x) not in ids:
                ids.append(int(x))
    ids = ids[:limit]
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT id, title, status, assignee, doer, blocked_reason, backlog_at FROM items WHERE id IN ({q})",
                        ids).fetchall()
    deps = conn.execute(f"SELECT d.item_id, d.blocked_by, b.status FROM deps d JOIN items b ON b.id=d.blocked_by "
                        f"WHERE d.item_id IN ({q}) AND d.kind IN ('blocks','feeds')", ids).fetchall()
    out = {}
    for r in rows:
        mine = [d for d in deps if d["item_id"] == r["id"]]
        out[str(r["id"])] = {"title": r["title"], "status": r["status"], "assignee": r["assignee"], "doer": r["doer"],
                             "blocked": bool(r["blocked_reason"]), "backlog": bool(r["backlog_at"]),
                             "waits": any(d["status"] in OPEN_STATES for d in mine),
                             "deps": [d["blocked_by"] for d in mine if d["blocked_by"] in ids]}
    return out


def inline_pictures(body, kind, base, max_bytes=3_000_000):
    """A page with each local picture that it names (Markdown ![..](shot.png), HTML <img src="shot.png">) put inline as
    a data: address, so the canvas shows it on any screen (#4983). base: the folder of the page's file. Returns
    (body, inlined count, the paths not found)."""
    import base64
    done, missing = [0], []

    def data(path):
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", path, re.I):
            return None
        f = Path(os.path.expanduser(path))
        f = f if f.is_absolute() else Path(base) / f
        t = _PICTURE_TYPES.get(f.suffix.lower())
        if not t:
            return None
        try:
            raw = f.read_bytes()
        except OSError:
            missing.append(path)
            return None
        if len(raw) > max_bytes:
            missing.append(f"{path} (more than {max_bytes // 1_000_000} MB)")
            return None
        done[0] += 1
        return f"data:{t};base64," + base64.b64encode(raw).decode()

    def md(m):
        d = data(m.group(2))
        return m.group(0) if d is None else f"![{m.group(1)}]({d}{m.group(3) or ''})"

    def img(m):
        d = data(m.group(3))
        return m.group(0) if d is None else f"{m.group(1)}{m.group(2)}{d}{m.group(2)}"

    if kind == "md":
        body = re.sub(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(\s+\"[^\"]*\")?\s*\)", md, body)
    body = re.sub(r"(<img\b[^>]*?\ssrc\s*=\s*)([\"'])([^\"']+)\2", img, body, flags=re.I)
    return body, done[0], missing


def canvas_kind(kind=None, filename=None):
    """The kind of a canvas: the one named, else from the file's extension."""
    if kind:
        kind = kind.lower().lstrip(".")
        kind = {"markdown": "md", "htm": "html"}.get(kind, kind)
        if kind not in CANVAS_KINDS:
            raise RiverError(f"a canvas is one of {', '.join(CANVAS_KINDS)}; got {kind!r}")
        return kind
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in CANVAS_EXT:
        return CANVAS_EXT[ext]
    raise RiverError("say the kind of the canvas: --kind html, md, or svg (or put a file that ends in .html, .md or .svg)")


def _canvas_row(conn, ref, project=None, deleted=False):
    """A canvas by id (12 or c12) or by name; a name needs its project when two projects have one."""
    ref = str(ref).strip()
    if re.fullmatch(r"c?\d+", ref):
        r = conn.execute("SELECT * FROM canvases WHERE id=?", (int(ref.lstrip("c")),)).fetchone()
        rows = [r] if r else []
    else:
        q = "SELECT c.* FROM canvases c JOIN projects p ON p.id=c.project_id WHERE c.name=?"
        args = [ref.lower()]
        if project:
            q += " AND p.name=?"
            args.append(project)
        rows = conn.execute(q, args).fetchall()
    if not deleted:
        rows = [r for r in rows if not r["deleted_at"]]
    if not rows:
        where = f" in project {project}" if project else ""
        raise RiverError(f"no canvas {ref}{where}; the canvases: maxpm canvas list")
    if len(rows) > 1:
        names = ", ".join(_project_name(conn, r["project_id"]) for r in rows)
        raise RiverError(f"canvas {ref} is in the projects {names}; name one: --project <name>")
    return rows[0]


def _canvas_latest(conn, cid):
    return conn.execute("SELECT * FROM canvas_versions WHERE canvas_id=? ORDER BY version DESC LIMIT 1",
                        (cid,)).fetchone()


def _held_item(conn, actor, project_id=None):
    """The one item that the actor holds (in progress or held), in the project when one is named; else None."""
    if not actor:
        return None
    q = "SELECT id, project_id FROM items WHERE assignee=? AND status IN ('in_progress','held')"
    args = [actor]
    if project_id is not None:
        q += " AND project_id=?"
        args.append(project_id)
    rows = conn.execute(q, args).fetchall()
    return rows[0] if len(rows) == 1 else None


def _canvas_info(conn, c):
    """A canvas row as a dict for list and show: its project, latest version, and comment counts."""
    d = dict(c)
    d["project"] = _project_name(conn, c["project_id"])
    v = _canvas_latest(conn, c["id"])
    d["version"] = v["version"] if v else 0
    d["bytes"] = len(v["body"].encode()) if v else 0
    counts = dict(conn.execute("SELECT state, COUNT(*) FROM canvas_comments WHERE canvas_id=? AND parent_id IS NULL "
                               "GROUP BY state", (c["id"],)).fetchall())
    d["drafts"] = counts.get("draft", 0)
    d["open_comments"] = counts.get("sent", 0) + counts.get("read", 0)
    d["item_title"] = (conn.execute("SELECT title FROM items WHERE id=?", (c["item_id"],)).fetchone() or [None])[0] \
        if c["item_id"] else None
    return d


def canvas_put(conn, name, body, actor=None, kind=None, title=None, item=None, project=None, note=None,
               filename=None):
    """Store a new version of a canvas (a new canvas when the name is new in its project). The project comes
    from the item, else the one named. The item is the one named, else the canvas's own, else the one item the
    actor holds in that project. The same text as the latest version stores nothing new."""
    if not actor:
        raise RiverError("putting a canvas needs an agent name: set MAXPM_AGENT or pass --as <name>")
    name = (name or "").strip().lower()
    if not _CANVAS_NAME.match(name):
        raise RiverError("a canvas name is 1 to 64 of a-z, 0-9, '.', '_' and '-', and starts with a letter or digit, "
                         "for example release-plan")
    if body is None or not str(body).strip():
        raise RiverError("the canvas is empty: put the page's text (a file, stdin, or --text)")
    body = str(body)
    with tx(conn):
        it = _item(conn, item) if item is not None else None
        if it is not None:
            pid = it["project_id"]
            if project and _project(conn, project)["id"] != pid:
                raise RiverError(f"item #{it['id']} is in project {_project_name(conn, pid)}, not {project}")
        elif project:
            pid = _project(conn, project)["id"]
        else:
            held = _held_item(conn, actor)
            if not held:
                raise RiverError("say the project of the canvas: --project <name> (or --item <id>; or run it in the "
                                 "project's folder)")
            # Only the project: the item is the canvas's own before the held one, below (#4841).
            pid = held["project_id"]
        c = conn.execute("SELECT * FROM canvases WHERE project_id=? AND name=?", (pid, name)).fetchone()
        kind = canvas_kind(kind, filename) if (kind or filename or not c) else c["kind"]
        if c and c["kind"] != kind and not c["deleted_at"]:
            raise RiverError(f"canvas {name} is {c['kind']}, not {kind}; use another name for a page of another kind")
        limit = int(setting(conn, "canvas_max", project_id=pid))
        size = len(body.encode())
        if size > limit:
            raise RiverError(f"the page has {size} bytes; a canvas version holds at most {limit} (setting canvas_max). "
                             f"Make images smaller, or link large files instead of putting them inline")
        if it is None and c is not None and c["item_id"]:
            it = _item(conn, c["item_id"])
        if it is None:
            held = _held_item(conn, actor, pid)
            it = _item(conn, held["id"]) if held else None
        t = iso(now())
        if c is None:
            cid = conn.execute("INSERT INTO canvases (project_id, name, title, kind, item_id, agent, created_at, "
                               "updated_at) VALUES (?,?,?,?,?,?,?,?)",
                               (pid, name, title, kind, it["id"] if it else None, actor, t, t)).lastrowid
        else:
            cid = c["id"]
            last = _canvas_latest(conn, cid)
            if last and last["body"] == body and not c["deleted_at"] and (title is None or title == c["title"]):
                if item is not None and it["id"] != c["item_id"]:
                    # The same text for another item: no new version, but the canvas moves to it (#4841).
                    conn.execute("UPDATE canvases SET item_id=?, updated_at=? WHERE id=?", (it["id"], t, cid))
                    _event(conn, it["id"], actor, f"canvas {name} moved here from "
                           f"{'#' + str(c['item_id']) if c['item_id'] else 'no item'}")
                return dict(canvas_show(conn, cid), unchanged=True)
            writer = c["agent"] if _is_person(conn, actor) and c["agent"] else actor  # a person's edit (#4769)
            conn.execute("UPDATE canvases SET title=COALESCE(?, title), kind=?, item_id=?, agent=?, updated_at=?, "
                         "deleted_at=NULL WHERE id=?", (title, kind, it["id"] if it else c["item_id"], writer, t, cid))
        n = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM canvas_versions WHERE canvas_id=?", (cid,)).fetchone()[0]
        conn.execute("INSERT INTO canvas_versions (canvas_id, version, body, author, note, created_at) VALUES (?,?,?,?,?,?)",
                     (cid, n, body, actor, (note or "").strip() or None, t))
        keep = int(setting(conn, "canvas_versions", project_id=pid))
        conn.execute("DELETE FROM canvas_versions WHERE canvas_id=? AND version>1 AND version<=? ",
                     (cid, n - max(keep, 2) + 1))
        _event(conn, it["id"] if it else None, actor, f"canvas {name} v{n} ({size} bytes)")
    return canvas_show(conn, cid)


_CANVAS_HOST = re.compile(r"^(https?://)?(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:\d{1,5})?$", re.I)


def canvas_hosts(value):
    """The setting canvas_network: None for on (any host), [] for off, else the list of hosts."""
    v = (value or "").strip()
    if v.lower() == "on":
        return None
    if v.lower() == "off":
        return []
    hosts = [h for h in re.split(r"[\s,]+", v) if h]
    bad = [h for h in hosts if not _CANVAS_HOST.match(h)]
    if bad or not hosts:
        raise RiverError("canvas_network is on, off, or hosts separated by spaces or commas, for example "
                         f"'cdn.jsdelivr.net cdnjs.cloudflare.com'{'; not a host: ' + ', '.join(bad) if bad else ''}")
    return hosts


def canvas_heads(conn):
    """Each canvas with its latest version number and no text: what the page polls to reload an open canvas, and
    what the Artifacts lists show (#4768). Newest change first."""
    return [dict(r) for r in conn.execute(
        "SELECT c.id, c.name, c.title, c.kind, p.name AS project, c.item_id, c.agent, c.updated_at, "
        "(SELECT MAX(version) FROM canvas_versions v WHERE v.canvas_id=c.id) AS version, "
        "(SELECT COUNT(*) FROM canvas_comments m WHERE m.canvas_id=c.id AND m.parent_id IS NULL "
        " AND m.state IN ('sent','read')) AS open_comments "
        "FROM canvases c JOIN projects p ON p.id=c.project_id WHERE c.deleted_at IS NULL "
        "ORDER BY c.updated_at DESC, c.id DESC")]


def canvas_version(conn, cid, version=None):
    """One version of a canvas by id (None or 'latest': the newest), with its canvas row; RiverError if gone."""
    c = conn.execute("SELECT * FROM canvases WHERE id=? AND deleted_at IS NULL", (int(cid),)).fetchone()
    if c is None:
        raise RiverError(f"no canvas c{cid}")
    if version in (None, "", "latest"):
        v = _canvas_latest(conn, c["id"])
    else:
        v = conn.execute("SELECT * FROM canvas_versions WHERE canvas_id=? AND version=?", (c["id"], int(version))).fetchone()
    if v is None:
        raise RiverError(f"canvas c{cid} has no version {version}")
    return c, v


def canvas_list(conn, project=None, item=None, mine=None, deleted=False):
    """The canvases, newest change first: all, a project's, an item's, or the ones an agent wrote last."""
    q, args = "SELECT * FROM canvases WHERE 1=1", []
    if project:
        q += " AND project_id=?"
        args.append(_project(conn, project)["id"])
    if item is not None:
        q += " AND item_id=?"
        args.append(int(item))
    if mine:
        q += " AND agent=?"
        args.append(mine)
    if not deleted:
        q += " AND deleted_at IS NULL"
    return [_canvas_info(conn, c) for c in conn.execute(q + " ORDER BY updated_at DESC, id DESC", args)]


def _comment_dict(r):
    d = dict(r)
    d["anchor"] = json.loads(r["anchor"]) if r["anchor"] else None
    return d


def canvas_show(conn, ref, project=None, version=None):
    """A canvas with the text of its latest version (or one version), its versions, and its comments."""
    c = _canvas_row(conn, ref, project)
    d = _canvas_info(conn, c)
    v = conn.execute("SELECT * FROM canvas_versions WHERE canvas_id=? AND version=?", (c["id"], int(version))).fetchone() \
        if version else _canvas_latest(conn, c["id"])
    if v is None:
        kept = [str(r[0]) for r in conn.execute("SELECT version FROM canvas_versions WHERE canvas_id=? ORDER BY version",
                                                (c["id"],))]
        raise RiverError(f"canvas {c['name']} has no version {version}; it keeps {', '.join(kept)}")
    d["shown"] = {"version": v["version"], "author": v["author"], "note": v["note"], "created_at": v["created_at"],
                  "body": v["body"]}
    d["versions"] = [dict(r) for r in conn.execute(
        "SELECT version, author, note, created_at, LENGTH(body) chars FROM canvas_versions WHERE canvas_id=? "
        "ORDER BY version", (c["id"],))]
    d["comments"] = _canvas_threads(conn, c["id"])
    return d


def _canvas_threads(conn, cid, open_only=False, include_drafts=True):
    """The comments of a canvas, each with its replies, open ones first, oldest first."""
    rows = [_comment_dict(r) for r in conn.execute("SELECT * FROM canvas_comments WHERE canvas_id=? ORDER BY id", (cid,))]
    replies = {}
    for r in rows:
        if r["parent_id"]:
            replies.setdefault(r["parent_id"], []).append(r)
    out = []
    for r in rows:
        if r["parent_id"]:
            continue
        if open_only and r["state"] not in ("sent", "read"):
            continue
        if not include_drafts and r["state"] == "draft":
            continue
        r["replies"] = replies.get(r["id"], [])
        out.append(r)
    order = {"sent": 0, "read": 0, "draft": 1, "resolved": 2}
    return sorted(out, key=lambda r: (order.get(r["state"], 3), r["id"]))


def _canvas_comment(conn, comment_id):
    r = conn.execute("SELECT * FROM canvas_comments WHERE id=?", (int(str(comment_id).lstrip("c#")),)).fetchone()
    if not r:
        raise RiverError(f"no canvas comment {comment_id}; the comments: maxpm canvas comments <canvas>")
    return r


def _anchor(anchor):
    """A comment's part of the page as stored JSON: {path, text, quote}; None is the whole page."""
    if anchor in (None, "", {}):
        return None
    if isinstance(anchor, str):
        try:
            anchor = json.loads(anchor)
        except ValueError:
            anchor = {"quote": anchor}
    if not isinstance(anchor, dict):
        raise RiverError("a comment's part is {path, text, quote} (or leave it out for the whole page)")
    a = {k: str(anchor[k])[:500] for k in ("path", "text", "quote") if anchor.get(k)}
    return json.dumps(a) if a else None


# The same comment again from the same person (the same canvas, part and words) within this time is one comment: a
# second click while the first one still runs, or a click again because nothing showed yet (#4845: answers came 2 to
# 6 times, 0 to 5 seconds apart).
COMMENT_AGAIN = timedelta(seconds=30)


def canvas_comment(conn, ref, body, actor=None, anchor=None, version=None, project=None, send_now=False):
    """A comment on a part of a canvas (anchor) or on the whole page. It is a draft until it is sent
    (canvas_send); send_now sends it at once. The same comment again within COMMENT_AGAIN gives back the first one
    (again: true), sent if send_now, and adds none."""
    if not actor:
        raise RiverError("commenting needs a name: set MAXPM_AGENT or pass --as <name>")
    if not body or not body.strip():
        raise RiverError("a comment needs text")
    c = _canvas_row(conn, ref, project)
    with tx(conn):
        latest = _canvas_latest(conn, c["id"])["version"]
        v = int(version) if version else latest
        if not conn.execute("SELECT 1 FROM canvas_versions WHERE canvas_id=? AND version=?", (c["id"], v)).fetchone():
            raise RiverError(f"canvas {c['name']} has no version {v}")
        same = conn.execute("SELECT id, state FROM canvas_comments WHERE canvas_id=? AND author=? AND parent_id IS NULL "
                            "AND body=? AND anchor IS ? AND created_at>=? ORDER BY id DESC LIMIT 1",
                            (c["id"], actor, body.strip(), _anchor(anchor), iso(now() - COMMENT_AGAIN))).fetchone()
        cid = same["id"] if same else conn.execute(
            "INSERT INTO canvas_comments (canvas_id, version, author, anchor, body, state, created_at) "
            "VALUES (?,?,?,?,?,'draft',?)", (c["id"], v, actor, _anchor(anchor), body.strip(), iso(now()))).lastrowid
    if send_now and (not same or same["state"] == "draft"):
        canvas_send(conn, c["id"], actor, [cid])
    return dict(_comment_dict(_canvas_comment(conn, cid)), **({"again": True} if same else {}))


def _canvas_writer(conn, c):
    """Who gets the comments of a canvas: the agent that put its newest version (a person's edit in between does
    not count, #4769) while its session is registered; else None (then they go to the holder of its item)."""
    r = conn.execute("SELECT v.author FROM canvas_versions v JOIN agents a ON a.name=v.author AND a.kind='ai' "
                     "WHERE v.canvas_id=? ORDER BY v.version DESC LIMIT 1", (c["id"],)).fetchone()
    who = r["author"] if r else c["agent"]
    r = conn.execute("SELECT kind FROM agents WHERE name=?", (who,)).fetchone() if who else None
    return who if r and r["kind"] == "ai" else None


def canvas_send(conn, ref, actor=None, comment_ids=None, project=None):
    """Send the drafts of a canvas (all of the actor's, or the ones named) as one message to the session that
    wrote it, or to the holder (or next holder) of its item. The message names the canvas, the version, each
    part, and the command that reads them."""
    if not actor:
        raise RiverError("sending needs a name: set MAXPM_AGENT or pass --as <name>")
    c = _canvas_row(conn, ref, project)
    if comment_ids:
        rows = [_canvas_comment(conn, x) for x in comment_ids]
        bad = [f"c{r['id']}" for r in rows if r["canvas_id"] != c["id"] or r["state"] != "draft" or r["parent_id"]]
        if bad:
            raise RiverError(f"not a draft comment of canvas {c['name']}: {', '.join(bad)}")
    else:
        rows = conn.execute("SELECT * FROM canvas_comments WHERE canvas_id=? AND state='draft' AND parent_id IS NULL "
                            "AND author=? ORDER BY id", (c["id"], actor)).fetchall()
    if not rows:
        raise RiverError(f"no draft comments on canvas {c['name']} to send; write one first: "
                         f"maxpm canvas comment {c['name']} \"...\"")
    to = _canvas_writer(conn, c)
    if to is None and not c["item_id"]:
        raise RiverError(f"the session that wrote canvas {c['name']} has ended and the canvas has no item, so nobody "
                         f"gets the comments; give it an item: maxpm canvas put ... --name {c['name']} --item <id>")
    latest = _canvas_latest(conn, c["id"])["version"]
    lines = [f"{len(rows)} comment(s) on canvas {c['name']} (version {latest}"
             + (f", {c['title']}" if c["title"] else "") + f", project {_project_name(conn, c['project_id'])}):"]
    for r in rows:
        a = json.loads(r["anchor"]) if r["anchor"] else None
        part = "the whole page"
        if a:
            part = (f"\"{a['quote']}\"" if a.get("quote") else f"\"{(a.get('text') or '')[:120]}\"") + \
                   (f" ({a['path']})" if a.get("path") else "")
        on = "" if r["version"] == latest else f", on version {r['version']}"
        lines.append(f"  c{r['id']} on {part}{on}: {r['body']}")
    lines.append(f"Read them: maxpm canvas comments {c['name']} --open; change the page (maxpm canvas put), then "
                 f"answer each: maxpm canvas reply c<id> \"...\" and maxpm canvas resolve c<id> --note \"...\"")
    text = "\n".join(lines)
    item, fresh = c["item_id"], None
    if to is None:
        # The session that wrote it has ended (#4769). An open item nobody holds: a fresh session takes it with the
        # comments in its notes (fresh_sessions). A closed item: a new item for the comments, which a fresh session
        # takes; the message goes with it.
        it = _item(conn, item)
        if it["status"] in CLOSED_STATES:
            new = item_add(conn, _project_name(conn, it["project_id"]), f"Answer the comments on canvas {c['name']}",
                           priority=it["priority"], actor=actor, found_during=item,
                           context=f"{text}\nThe canvas came from #{item}: read it first (maxpm show {item}).")
            item = new["id"]
        with tx(conn):
            fresh = _want_fresh_comments(conn, item, text)
    msg = send(conn, "note", text, to=to, item=item, actor=actor)
    with tx(conn):
        t = iso(now())
        for r in rows:
            conn.execute("UPDATE canvas_comments SET state='sent', sent_at=?, message_id=? WHERE id=?",
                         (t, msg["id"], r["id"]))
    return {"canvas": c["name"], "sent": [r["id"] for r in rows], "message": msg["id"], "item": item,
            "to": msg.get("to_agent") or (f"a fresh session for #{item}" if fresh else f"the next holder of #{item}")}


def _want_fresh_comments(conn, item_id, text):
    """Comments for an agent item that nobody holds: maxpm serve starts a fresh session for it (fresh_sessions), as
    for an answer. Inside a tx. False when it is held, reserved, waits, or the setting is off."""
    it = _item(conn, item_id)
    if it["status"] != "open" or it["doer"] == "human" or it["reserved_for"] or it["assignee"]:
        return False
    if setting(conn, "fresh_sessions", item_id=item_id) != "on":
        return False
    conn.execute("UPDATE items SET fresh_start=? WHERE id=?", (iso(now()), item_id))
    _event(conn, item_id, "maxpm", "canvas comments came: a fresh session takes it (fresh_sessions)")
    return True


def canvas_discard(conn, comment_id, actor=None):
    """Delete a draft comment of one's own (it was never sent)."""
    r = _canvas_comment(conn, comment_id)
    if r["state"] != "draft" or r["author"] != actor:
        raise RiverError(f"c{r['id']} is not a draft of yours; a sent comment stays (resolve it: "
                         f"maxpm canvas resolve c{r['id']} --note \"...\")")
    with tx(conn):
        conn.execute("DELETE FROM canvas_comments WHERE id=?", (r["id"],))
    return {"discarded": r["id"]}


def canvas_edit(conn, ref, body, actor=None, project=None):
    """A person changes a Markdown canvas in the page (#4769): a new version by that person; the session that
    wrote it gets a note, so it reads the change before its next version."""
    c = _canvas_row(conn, ref, project)
    if c["kind"] != "md":
        raise RiverError(f"canvas {c['name']} is {c['kind']}: only a Markdown canvas has an editor in the page; "
                         "comment on it, and the session changes it")
    res = canvas_put(conn, c["name"], body, actor, project=_project_name(conn, c["project_id"]), item=c["item_id"],
                     note=f"edited by {actor}")
    to = _canvas_writer(conn, c)
    if not res.get("unchanged") and (to or c["item_id"]) and to != actor:
        send(conn, "note", f"{actor} edited canvas {c['name']} in the page: version {res['version']}. Read it before "
             f"your next version: maxpm canvas show {c['name']}", to=to, item=c["item_id"], actor=actor, level="low")
    return res


def canvas_comments(conn, ref, actor=None, open_only=False, project=None):
    """The comments of a canvas with their replies. When an agent reads them, the sent ones become read."""
    c = _canvas_row(conn, ref, project)
    if actor and not _is_person(conn, actor):
        with tx(conn):
            conn.execute("UPDATE canvas_comments SET state='read', read_at=? WHERE canvas_id=? AND state='sent' "
                         "AND author<>?", (iso(now()), c["id"], actor))
    threads = _canvas_threads(conn, c["id"], open_only, include_drafts=not open_only)
    return {"canvas": c["name"], "id": c["id"], "project": _project_name(conn, c["project_id"]),
            "version": _canvas_latest(conn, c["id"])["version"], "comments": threads}


def canvas_reply(conn, comment_id, body, actor=None):
    """A reply in the thread of a comment. It goes to the comment's author as a note too."""
    if not actor:
        raise RiverError("replying needs a name: set MAXPM_AGENT or pass --as <name>")
    if not body or not body.strip():
        raise RiverError("a reply needs text")
    top = _canvas_comment(conn, comment_id)
    if top["parent_id"]:
        top = _canvas_comment(conn, top["parent_id"])
    c = _canvas_row(conn, top["canvas_id"], deleted=True)
    with tx(conn):
        v = _canvas_latest(conn, c["id"])["version"]
        rid = conn.execute("INSERT INTO canvas_comments (canvas_id, version, parent_id, author, body, state, created_at) "
                           "VALUES (?,?,?,?,?,'reply',?)", (c["id"], v, top["id"], actor, body.strip(), iso(now()))).lastrowid
        if top["state"] == "sent" and actor != top["author"]:
            conn.execute("UPDATE canvas_comments SET state='read', read_at=? WHERE id=?", (iso(now()), top["id"]))
    if top["author"] and top["author"] != actor and conn.execute("SELECT 1 FROM agents WHERE name=?",
                                                                 (top["author"],)).fetchone():
        send(conn, "note", f"Reply on canvas {c['name']} to your comment c{top['id']} (\"{top['body'][:80]}\"): "
             f"{body.strip()}", to=top["author"], item=c["item_id"], actor=actor, level="low")
    return _comment_dict(_canvas_comment(conn, rid))


def canvas_resolve(conn, comment_id, actor=None, note=None):
    """Close a comment: the change is made (say in note which version), or it needs none (say why)."""
    if not actor:
        raise RiverError("resolving needs a name: set MAXPM_AGENT or pass --as <name>")
    r = _canvas_comment(conn, comment_id)
    if r["parent_id"]:
        raise RiverError(f"c{r['id']} is a reply; resolve its comment c{r['parent_id']}")
    if r["state"] == "resolved":
        raise RiverError(f"comment c{r['id']} is already resolved")
    with tx(conn):
        conn.execute("UPDATE canvas_comments SET state='resolved', resolved_at=?, resolved_by=?, resolve_note=? WHERE id=?",
                     (iso(now()), actor, (note or "").strip() or None, r["id"]))
    return _comment_dict(_canvas_comment(conn, r["id"]))


# Docs (#4708): a doc is a Markdown canvas that a session writes section by section, as on claude.ai. A section is a
# heading and the lines up to the next heading of the same or a higher level; a section whose text is one line
# "writing: <intent>" is pending, and the page shows it as being written.
_MD_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_DECISION = re.compile(r"^\W*Decision (\d+) of (\d+):\s*(.+?)\s*(?:\(item #\d+\))?\s*$", re.I)


def md_sections(text):
    """The sections of a Markdown text: [{title, level, start, end}] with line numbers (end: the first line after the
    section); headings inside fenced code do not count."""
    lines, out, fence = text.split("\n"), [], None
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(```|~~~)", line)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
            continue
        h = None if fence else _MD_HEADING.match(line)
        if h:
            out.append({"title": h.group(2).strip(), "level": len(h.group(1)), "start": i, "end": len(lines)})
    for k, s in enumerate(out):
        nxt = next((x for x in out[k + 1:] if x["level"] <= s["level"]), None)
        s["end"] = nxt["start"] if nxt else len(lines)
    return out


def canvas_section(conn, ref, heading, body=None, actor=None, project=None, note=None, pending=None, level=2):
    """Replace one section of a Markdown canvas (its heading stays) as a new version, or add it at the end when no
    section has that heading. pending: the section shows as being written ("writing: <intent>"). A person's change of a
    section sends a note to the session that writes the doc (#4708)."""
    c = _canvas_row(conn, ref, project)
    if c["kind"] != "md":
        raise RiverError(f"canvas {c['name']} is {c['kind']}: sections are for a Markdown canvas (a doc)")
    heading = re.sub(r"^#+\s*", "", (heading or "").strip())
    if not heading:
        raise RiverError("say the section's heading, for example 'Decisions'")
    if pending is not None:
        body = f"writing: {pending.strip() or 'this section'}"
    if body is None or not str(body).strip():
        raise RiverError("give the section's text (a file, stdin, or --text), or --pending '<what it will say>'")
    body = str(body).strip("\n")
    if _MD_HEADING.match(body.split("\n", 1)[0]):  # the text came with its own heading: keep the old one
        first = _MD_HEADING.match(body.split("\n", 1)[0])
        if first.group(2).strip().lower() == heading.lower():
            body = body.split("\n", 1)[1].strip("\n") if "\n" in body else ""
    text = _canvas_latest(conn, c["id"])["body"]
    lines = text.split("\n")
    found = next((s for s in md_sections(text) if s["title"].lower() == heading.lower()), None)
    if found:
        tail = lines[found["end"]:]
        new = lines[:found["start"] + 1] + [""] + body.split("\n") + ([""] if tail else []) + tail
    else:
        new = lines + ([""] if lines and lines[-1].strip() else []) + [f"{'#' * level} {heading}", ""] + body.split("\n")
    res = canvas_put(conn, c["name"], "\n".join(new).rstrip("\n") + "\n", actor,
                     project=_project_name(conn, c["project_id"]), item=c["item_id"],
                     note=note or (f"section {heading}" + (" (pending)" if pending is not None else "")))
    if _is_person(conn, actor) and not res.get("unchanged"):
        to = _canvas_writer(conn, c)
        if to or c["item_id"]:
            send(conn, "note", f"{actor} changed the section '{heading}' of doc {c['name']} in the page: version "
                 f"{res['version']}. Read it before your next version: maxpm canvas show {c['name']}",
                 to=to, item=c["item_id"], actor=actor, level="low")
    return dict(res, section=heading, added=not found)


def doc_decisions(text):
    """The decisions of a doc in the decision form (maxpm guide decisions): [{n, of, name, answer_hint}]."""
    out = []
    for line in text.split("\n"):
        m = _DECISION.match(line.strip())
        if m:
            out.append({"n": int(m.group(1)), "of": int(m.group(2)), "name": m.group(3).strip()})
    return out


def canvas_answer(conn, ref, decision, answer, actor=None, project=None):
    """A person's answer to a decision of a doc, in place (#4708): a comment on the decision, sent at once to the
    session that writes the doc (or a fresh session for its item), as maxpm answer does for a question."""
    c = _canvas_row(conn, ref, project)
    if not answer or not str(answer).strip():
        raise RiverError("an answer needs words: the option, or what you decide")
    body = _canvas_latest(conn, c["id"])["body"]
    if c["kind"] == "html":  # a kit decision card (#4983): its heading holds "Decision <n> of <m>: <name>"
        import html as _html
        body = _html.unescape(re.sub(r"<[^>]+>", "\n", body))
    ds = {d["n"]: d for d in doc_decisions(body)}
    n = int(str(decision).lstrip("#").replace("decision-", "").replace("decision:", ""))
    if n not in ds:
        raise RiverError(f"doc {c['name']} has no decision {n}; it has {', '.join(map(str, sorted(ds))) or 'none'}")
    d = ds[n]
    com = canvas_comment(conn, c["id"], f"Answer to decision {n} of {d['of']} ({d['name']}): {str(answer).strip()}",
                         actor, {"path": f"decision:{n}", "text": f"Decision {n} of {d['of']}: {d['name']}"},
                         send_now=True)
    return dict(com, decision=n, name=d["name"])


def canvas_delete(conn, ref, actor=None, project=None):
    """Delete a canvas: it leaves every list. Its versions stay in the database, and a put of the same name in
    the same project brings it back with a new version."""
    c = _canvas_row(conn, ref, project)
    with tx(conn):
        conn.execute("UPDATE canvases SET deleted_at=? WHERE id=?", (iso(now()), c["id"]))
        _event(conn, c["item_id"], actor, f"canvas {c['name']} deleted")
    return {"canvas": c["name"], "id": c["id"], "deleted": True}


def canvas_save(conn, ref, path, version=None, project=None, force=False):
    """Write a version of a canvas as a file (for a page that must go into git)."""
    d = canvas_show(conn, ref, project, version)
    p = Path(os.path.expanduser(str(path).strip()))
    if p.is_dir():
        p = p / f"{d['name']}.{d['kind']}"
    if p.exists() and not force:
        raise RiverError(f"{p} exists; add --force to write over it")
    try:
        p.write_text(d["shown"]["body"])
    except OSError as e:
        raise RiverError(f"cannot write {p}: {e.strerror}")
    return {"canvas": d["name"], "version": d["shown"]["version"], "path": str(p), "bytes": d["bytes"]}


# ---------------------------------------------------------------- session samples (#5352)

SAMPLE_STATES = ("working", "waiting_person", "waiting_work", "manager", "expert")
# A pass that comes late (serve was down, the machine slept) counts at most this many sample_every for its rows,
# so a night with serve down does not show as hours of idle time.
SAMPLE_GAP_MAX = 5


def _sample_state(conn, a, held, waiting):
    """The state of one live AI session for a sample: (state, item_id, project_id)."""
    it = held.get(a["name"])
    if a["name"] in waiting:
        return "waiting_person", it and it["id"], it and it["project_id"]
    if it:
        return "working", it["id"], it["project_id"]
    role = a["role"] or ""
    if role == "expert":
        p = conn.execute("SELECT id FROM projects WHERE name=?", (a["expert_project"],)).fetchone() \
            if a["expert_project"] else None
        return "expert", None, p and p["id"]
    if role == "manager":
        return "manager", None, None
    # The project of the item it held last: an idle worker counts against the project it works in.
    p = conn.execute("SELECT project_id FROM items WHERE assignee=? ORDER BY COALESCE(claimed_at, created_at) DESC "
                     "LIMIT 1", (a["name"],)).fetchone()
    return "waiting_work", None, p and p["project_id"]


def sample_sessions(conn, at=None):
    """One pass of the loop of maxpm serve: record the state of each live AI session in session_samples, at most
    once per sample_every, and delete rows older than sample_keep. Returns the number of rows it wrote (0 when the
    last sample is too new, or sample_every is 0s). Working: it holds an item (in progress or held). Waiting_person:
    it waits at its prompt for a person's answer (waits_on_person). Waiting_work: a worker with nothing in hand.
    A manager and an expert hold no item by design, so they have states of their own."""
    every = parse_duration(setting(conn, "sample_every")).total_seconds()
    if not every:
        return 0
    at = at or now()
    last = conn.execute("SELECT MAX(at) AS at FROM session_samples").fetchone()["at"]
    gap = (at - parse_iso(last)).total_seconds() if last else every
    if gap < every * 0.9:  # a pass a little early (notify_interval does not divide sample_every) still samples
        return 0
    secs = int(round(min(max(gap, 0), every * SAMPLE_GAP_MAX)))
    live = [a for a in conn.execute("SELECT * FROM agents WHERE kind='ai' AND COALESCE(role, '')<>'assistant'")
            if _agent_state(conn, a) not in ("gone", "stopped")]
    held = {}
    for it in conn.execute("SELECT id, project_id, assignee FROM items WHERE status IN ('in_progress','held') "
                           "AND assignee IS NOT NULL ORDER BY status='held', claimed_at DESC"):
        held.setdefault(it["assignee"], it)  # an item in progress before a held one: what it works on now
    waiting = waits_on_person(conn) if live else {}
    keep = parse_duration(setting(conn, "sample_keep"))
    with tx(conn):
        for a in live:
            state, item_id, project_id = _sample_state(conn, a, held, waiting)
            conn.execute("INSERT INTO session_samples (at, agent, state, role, item_id, project_id, secs) "
                         "VALUES (?,?,?,?,?,?,?)", (iso(at), a["name"], state, a["role"], item_id, project_id, secs))
        if not live:
            # No session: write nothing, but remember the time, so the first sample after a quiet hour counts one
            # sample_every and not the hour.
            conn.execute("INSERT INTO session_samples (at, agent, state, secs) VALUES (?, '', 'none', 0)", (iso(at),))
        conn.execute("DELETE FROM session_samples WHERE at<?", (iso(at - keep),))
    return len(live)


def idle_share(conn, since="7d", until=None, project=None):
    """The share of sampled session time in each state, from since (a duration back from now, or a time) to until.
    project: only the rows of that project (by the item held, else the item held last). Returns a dict: secs and
    share per state, total_secs, sessions (distinct agents), samples, idle (the waiting_work share: the time a
    worker session had nothing to do), and first (the oldest sample, so a reader knows how far back it goes)."""
    t1 = until if isinstance(until, datetime) else (parse_when(until) if until else now())
    t0 = since if isinstance(since, datetime) else (t1 - parse_duration(since) if _DURATION.match(since or "")
                                                    else parse_when(since))
    sql = "SELECT state, SUM(secs) AS secs, COUNT(*) AS n, COUNT(DISTINCT agent) AS agents FROM session_samples " \
          "WHERE at>=? AND at<? AND state<>'none'"
    args = [iso(t0), iso(t1)]
    if project is not None:
        sql += " AND project_id=?"
        args.append(_project(conn, project)["id"])
    rows = conn.execute(sql + " GROUP BY state", args).fetchall()
    by = {s: 0 for s in SAMPLE_STATES}
    for r in rows:
        by[r["state"]] = by.get(r["state"], 0) + (r["secs"] or 0)
    total = sum(by.values())
    sessions = conn.execute("SELECT COUNT(DISTINCT agent) AS n FROM session_samples WHERE at>=? AND at<? "
                            "AND state<>'none'" + (" AND project_id=?" if project is not None else ""),
                            args).fetchone()["n"]
    first = conn.execute("SELECT MIN(at) AS at FROM session_samples WHERE state<>'none'").fetchone()["at"]
    share = {s: (v / total if total else 0.0) for s, v in by.items()}
    return {"since": iso(t0), "until": iso(t1), "secs": by, "share": share, "total_secs": total,
            "sessions": sessions, "samples": sum(r["n"] for r in rows), "idle": share["waiting_work"],
            "first": first}


# ---------------------------------------------------------------- health (#5353)

# A done item of a review fail: review_fail adds each fix with this context (the rework a review found).
REVIEW_FIX_CONTEXT = "Found in the review of release"


def _pct(values, q):
    """The q quantile (0..1) of a list of numbers, by the nearest rank; None for an empty list."""
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, max(0, math.ceil(q * len(v)) - 1))]


def _health_range(conn, t0, t1, pid, minutes, with_cost=True, sessions=True):
    """The measures of maxpm health for the time from t0 to t1 (pid: one project's id, or None)."""
    a, b = iso(t0), iso(t1)
    pw = " AND i.project_id=?" if pid is not None else ""
    pa = [pid] if pid is not None else []
    days = max((t1 - t0).total_seconds() / 86400, 1 / 24)
    done = conn.execute("SELECT i.id, i.kind, i.doer, p.name AS project FROM items i JOIN projects p ON p.id=i.project_id "
                        "WHERE i.status='done' AND i.closed_at>=? AND i.closed_at<?" + pw, [a, b] + pa).fetchall()
    n = len(done)
    by_kind, by_project = {}, {}
    for r in done:
        by_kind[r["kind"] or "work"] = by_kind.get(r["kind"] or "work", 0) + 1
        by_project[r["project"]] = by_project.get(r["project"], 0) + 1

    def events(like):
        return conn.execute("SELECT COUNT(*) AS n FROM events e JOIN items i ON i.id=e.item_id "
                            "WHERE e.change LIKE ? AND e.at>=? AND e.at<?" + pw, [like, a, b] + pa).fetchone()["n"]
    reopens = events("reopened%")
    lost = events("lease expired%")
    fixes = conn.execute("SELECT COUNT(*) AS n FROM items i WHERE i.context LIKE ? AND i.created_at>=? "
                         "AND i.created_at<?" + pw, [REVIEW_FIX_CONTEXT + "%", a, b] + pa).fetchone()["n"]
    # Needs-you rows: the person's part. A message row counts for the project of its message's item.
    ny = ("SELECT n.opened_at, n.closed_at FROM needs_you n LEFT JOIN messages m ON m.id=n.message_id "
          "LEFT JOIN items i ON i.id=COALESCE(n.item_id, m.item_id) WHERE 1=1" + pw)
    opened = conn.execute(f"SELECT COUNT(*) AS n FROM ({ny}) WHERE opened_at>=? AND opened_at<?", pa + [a, b]).fetchone()["n"]
    waits = [(parse_iso(r["closed_at"]) - parse_iso(r["opened_at"])).total_seconds() / 3600 for r in conn.execute(
        f"SELECT * FROM ({ny}) WHERE closed_at>=? AND closed_at<?", pa + [a, b])]
    open_ages = [(t1 - parse_iso(r["opened_at"])).total_seconds() / 3600 for r in conn.execute(
        f"SELECT * FROM ({ny}) WHERE opened_at<? AND (closed_at IS NULL OR closed_at>=?)", pa + [b, b])]
    # Cost: the measured usage of the items done in the range, in input-token equivalents.
    cost, cost_kind = [], {}
    for r in (done if with_cost else ()):
        u = item_usage(conn, r["id"])
        if u:
            cost.append(u["eq"])
            cost_kind.setdefault(r["kind"] or "work", []).append(u["eq"])
    med = (lambda xs: statistics.median(xs) if xs else None)
    return {
        "since": a, "until": b, "days": round(days, 2), "done": n,
        "per_day": n / days, "by_kind": by_kind, "by_project": by_project,
        "reopens": reopens, "lost_claims": lost, "review_fixes": fixes,
        "rework_rate": (reopens + lost + fixes) / n if n else None,
        "needs_you_opened": opened,
        "person_load": opened * 100 / n if n else None,
        "person_minutes_per_item": opened * minutes / n if n else None,
        "wait_closed": len(waits),
        "wait_mean_h": statistics.fmean(waits) if waits else None, "wait_p90_h": _pct(waits, 0.9),
        "open_now": len(open_ages), "open_mean_age_h": statistics.fmean(open_ages) if open_ages else None,
        "cost_measured": len(cost), "cost_median_eq": med(cost),
        "cost_mean_eq": statistics.fmean(cost) if cost else None,
        "cost_by_kind": {k: {"items": len(v), "median_eq": med(v)} for k, v in sorted(cost_kind.items())},
        "sessions": idle_share(conn, t0, t1, project=None if pid is None else
                               conn.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()["name"])
        if sessions else None,
    }


def _may_log_change(conn, actor):
    if not actor:
        raise RiverError("name yourself: maxpm --as <name> change ...")
    r = conn.execute("SELECT kind, role FROM agents WHERE name=?", (actor,)).fetchone()
    if not (r and (r["kind"] == "human" or r["role"] == "manager")):
        raise RiverError(f"{actor} is an agent; a person or the manager keeps the change log. Ask the manager: "
                         f"maxpm --as {actor} note <manager> \"change log: <name> at <time>\"")


def change_add(conn, name, at=None, note="", actor=None):
    """maxpm change add: record that a change to the way of work shipped at `at` (default now), so maxpm health
    shows the measures before and after it."""
    name = (name or "").strip()
    if not name:
        raise RiverError("a change needs a name: maxpm change add \"<what changed>\" [--at <time>]")
    _may_log_change(conn, actor)
    t = (parse_when(at) if isinstance(at, str) else at) if at else now()
    if t > now() + timedelta(minutes=1):
        raise RiverError(f"a change in the change log shipped already: {iso(t)} is in the future")
    with tx(conn):
        cid = conn.execute("INSERT INTO changes (at, name, note, actor, created_at) VALUES (?,?,?,?,?)",
                           (iso(t), name, note or None, actor, iso(now()))).lastrowid
        _event(conn, None, actor, f"change log: {name} at {iso(t)}")
    return dict(conn.execute("SELECT * FROM changes WHERE id=?", (cid,)).fetchone())


def change_list(conn):
    """The change log, oldest first."""
    return [dict(r) for r in conn.execute("SELECT * FROM changes ORDER BY at, id")]


def change_remove(conn, change_id, actor=None):
    """maxpm change remove: take a wrong row out of the change log."""
    _may_log_change(conn, actor)
    with tx(conn):
        r = conn.execute("SELECT * FROM changes WHERE id=?", (int(str(change_id).lstrip("c")),)).fetchone()
        if not r:
            raise RiverError(f"no change {change_id} in the change log; see: maxpm change list")
        conn.execute("DELETE FROM changes WHERE id=?", (r["id"],))
        _event(conn, None, actor, f"change log: removed {r['name']} at {r['at']}")
    return dict(r)


def _change_effects(conn, t0, t1, pid, minutes):
    """Each change in the change log from t0 to t1, with the headline, throughput and rework rate for the time of
    the same length as t1 - t0 before and after it, each cut at the change next to it (and after it at t1)."""
    span, end = t1 - t0, now()
    rows = change_list(conn)
    out = []
    for i, c in enumerate(rows):
        at = parse_iso(c["at"])
        if not (t0 <= at <= t1):
            continue
        start = max(at - span, parse_iso(rows[i - 1]["at"])) if i else at - span
        stop = min(at + span, parse_iso(rows[i + 1]["at"])) if i + 1 < len(rows) else at + span
        stop = min(stop, end, t1)
        sides = {}
        for side, (a, b) in (("before", (start, at)), ("after", (at, stop))):
            if b <= a:
                sides[side] = None
                continue
            r = _health_range(conn, a, b, pid, minutes, with_cost=False, sessions=False)
            sides[side] = {k: r[k] for k in ("since", "until", "days", "done", "per_day", "rework_rate",
                                             "person_minutes_per_item")}
        out.append({**c, **sides})
    return out


# A group of the goal comparison with fewer done items than this is too small to read (#5355).
HEALTH_MIN_GROUP = 20


def _goal_split(conn, t0, t1, pid):
    """maxpm health --by goal (#5355, decision 3 of does-maxpm-work): the agent task items done from t0 to t1 with
    a goal tag against those without one, per project that has both, then a total in which each project weighs as
    its smaller group, so neither the type of work of a project nor a group of two items decides it. Per group: done items,
    median minutes from claim to done, rework per item (its reopens and lost claims), person load (needs-you rows
    per 100 items) and the median cost. A tag counts as it is now (the queue keeps no history of tags)."""
    a, b = iso(t0), iso(t1)
    days = max((t1 - t0).total_seconds() / 86400, 1 / 24)
    pw = " AND i.project_id=?" if pid is not None else ""
    rows = conn.execute(
        "SELECT i.id, i.claimed_at, i.closed_at, p.name AS project, "
        "EXISTS (SELECT 1 FROM item_goals g WHERE g.item_id=i.id) AS goal, "
        "(SELECT COUNT(*) FROM events e WHERE e.item_id=i.id AND (e.change LIKE 'reopened%' "
        "  OR e.change LIKE 'lease expired%')) AS rework, "
        "(SELECT COUNT(*) FROM needs_you n LEFT JOIN messages m ON m.id=n.message_id "
        "  WHERE COALESCE(n.item_id, m.item_id)=i.id) AS ny "
        "FROM items i JOIN projects p ON p.id=i.project_id WHERE i.status='done' AND i.closed_at>=? AND i.closed_at<? "
        "AND COALESCE(i.kind, 'work') IN ('work', 'operational') AND i.doer IN ('ai', 'any')" + pw,
        [a, b] + ([pid] if pid is not None else [])).fetchall()

    def stats(items):
        n = len(items)
        mins = [(parse_iso(r["closed_at"]) - parse_iso(r["claimed_at"])).total_seconds() / 60
                for r in items if r["claimed_at"] and r["closed_at"] >= r["claimed_at"]]
        eqs = [u["eq"] for u in (item_usage(conn, r["id"]) for r in items) if u]
        return {"done": n, "per_day": n / days, "small": n < HEALTH_MIN_GROUP,
                "median_minutes": statistics.median(mins) if mins else None,
                "rework_rate": sum(r["rework"] for r in items) / n if n else None,
                "person_load": sum(r["ny"] for r in items) * 100 / n if n else None,
                "median_eq": statistics.median(eqs) if eqs else None, "cost_measured": len(eqs)}
    by_project = {}
    for r in rows:
        by_project.setdefault(r["project"], {True: [], False: []})[bool(r["goal"])].append(r)
    projects = []
    for name, g in sorted(by_project.items(), key=lambda kv: -len(kv[1][True]) - len(kv[1][False])):
        if g[True] and g[False]:
            projects.append({"project": name, "goal": stats(g[True]), "none": stats(g[False])})
    keys = ("median_minutes", "rework_rate", "person_load", "median_eq")
    total = {}
    for side in ("goal", "none"):
        t = {"done": sum(p[side]["done"] for p in projects)}
        t["per_day"] = t["done"] / days
        t["small"] = t["done"] < HEALTH_MIN_GROUP
        for k in keys:
            # Each project weighs as its smaller group: a project with 2 goal items and 350 others tells little
            # about goals, and its 2-item median must not count as 352 items.
            w = [(min(p["goal"]["done"], p["none"]["done"]), p[side][k]) for p in projects if p[side][k] is not None]
            t[k] = sum(x * v for x, v in w) / sum(x for x, _ in w) if w else None
        total[side] = t
    one_sided = {"goal": sum(len(g[True]) for g in by_project.values() if not g[False]),
                 "none": sum(len(g[False]) for g in by_project.values() if not g[True])}
    return {"projects": projects, "total": total, "left_out": one_sided, "min_group": HEALTH_MIN_GROUP}


def _health_days(conn, t0, t1, pid, minutes):
    """The measures of maxpm health per day (UTC) from t0 to t1, for the graphs of the Health view (#5356): done
    items, rework (reopens, lost claims, review fixes), needs-you rows opened, person minutes per done item, the
    idle share of the sampled session time, and the median cost of the items done that day."""
    a, b = iso(t0), iso(t1)
    pw = " AND i.project_id=?" if pid is not None else ""
    pa = [pid] if pid is not None else []
    day = {}
    d, last = t0.date(), (t1 - timedelta(seconds=1)).date()
    while d <= last:
        day[d.isoformat()] = {"day": d.isoformat(), "done": 0, "rework": 0, "needs_you": 0, "eqs": [],
                              "idle": None, "sampled_secs": 0}
        d += timedelta(days=1)

    def add(sql, args, key):
        for r in conn.execute(sql, args):
            if r["d"] in day:
                day[r["d"]][key] += r["n"]
    add("SELECT substr(i.closed_at, 1, 10) AS d, COUNT(*) AS n FROM items i WHERE i.status='done' "
        "AND i.closed_at>=? AND i.closed_at<?" + pw + " GROUP BY d", [a, b] + pa, "done")
    add("SELECT substr(e.at, 1, 10) AS d, COUNT(*) AS n FROM events e JOIN items i ON i.id=e.item_id "
        "WHERE (e.change LIKE 'reopened%' OR e.change LIKE 'lease expired%') AND e.at>=? AND e.at<?" + pw
        + " GROUP BY d", [a, b] + pa, "rework")
    add("SELECT substr(i.created_at, 1, 10) AS d, COUNT(*) AS n FROM items i WHERE i.context LIKE ? "
        "AND i.created_at>=? AND i.created_at<?" + pw + " GROUP BY d", [REVIEW_FIX_CONTEXT + "%", a, b] + pa, "rework")
    add("SELECT substr(n.opened_at, 1, 10) AS d, COUNT(*) AS n FROM needs_you n LEFT JOIN messages m "
        "ON m.id=n.message_id LEFT JOIN items i ON i.id=COALESCE(n.item_id, m.item_id) WHERE n.opened_at>=? "
        "AND n.opened_at<?" + pw + " GROUP BY d", [a, b] + pa, "needs_you")
    for r in conn.execute(
            "SELECT substr(i.closed_at, 1, 10) AS d, u.input + ? * u.cache_read + ? * u.write_1h + ? * u.write_5m "
            "AS eq FROM items i JOIN (SELECT item_id, SUM(input) AS input, SUM(cache_read) AS cache_read, "
            "SUM(write_1h) AS write_1h, SUM(write_5m) AS write_5m FROM item_usage GROUP BY item_id) u "
            "ON u.item_id=i.id WHERE i.status='done' AND i.closed_at>=? AND i.closed_at<?" + pw,
            [EQ_READ, EQ_WRITE_1H, EQ_WRITE_5M, a, b] + pa):
        if r["d"] in day:
            day[r["d"]]["eqs"].append(r["eq"])
    sw = " AND project_id=?" if pid is not None else ""
    for r in conn.execute("SELECT substr(at, 1, 10) AS d, SUM(secs) AS total, "
                          "SUM(CASE WHEN state='waiting_work' THEN secs ELSE 0 END) AS idle FROM session_samples "
                          "WHERE at>=? AND at<? AND state<>'none'" + sw + " GROUP BY d", [a, b] + pa):
        if r["d"] in day and r["total"]:
            day[r["d"]]["idle"] = r["idle"] / r["total"]
            day[r["d"]]["sampled_secs"] = r["total"]
    out = []
    for v in day.values():
        eqs = v.pop("eqs")
        n = v["done"]
        v["median_eq"] = statistics.median(eqs) if eqs else None
        v["rework_rate"] = v["rework"] / n if n else None
        v["person_minutes_per_item"] = v["needs_you"] * minutes / n if n else None
        out.append(v)
    return out


def health(conn, since="7d", until=None, project=None, by=None, days=False):
    """maxpm health: does MaximizePM work? The measures of the canvas does-maxpm-work (decided 2026-11-11, #5338)
    for the time from since to until, and for the time of the same length before it, so a change shows. Headline:
    person minutes per done item (a proxy: needs-you rows x health_person_minutes / items done), with items done
    per day and the rework rate beside it, so a fall in quality shows. Rework: reopens, lost claims (a lease ran
    out) and the fix items of a failed review, per done item. Cost in input-token equivalents (maxpm usage)."""
    # Default end: the start of the next second, as iso() keeps whole seconds: an item closed this second counts.
    t1 = until if isinstance(until, datetime) else (parse_when(until) if until else
                                                    now().replace(microsecond=0) + timedelta(seconds=1))
    if since == "all":
        first = conn.execute("SELECT MIN(created_at) AS t FROM items").fetchone()["t"]
        t0 = parse_iso(first) if first else t1
    else:
        t0 = since if isinstance(since, datetime) else (t1 - parse_duration(since) if _DURATION.match(since or "")
                                                        else parse_when(since))
    if t0 >= t1:
        raise RiverError("maxpm health: since must be before until (for example --since 7d, or --since 30d)")
    pid = _project(conn, project)["id"] if project else None
    minutes = int(setting(conn, "health_person_minutes", project_id=pid))
    now_r = _health_range(conn, t0, t1, pid, minutes)
    before = _health_range(conn, t0 - (t1 - t0), t0, pid, minutes)
    res = {"project": project, "person_minutes": minutes, "now": now_r, "before": before,
           "changes": _change_effects(conn, t0, t1, pid, minutes)}
    if days:
        res["days"] = _health_days(conn, t0, t1, pid, minutes)
    if by == "goal":
        res["by_goal"] = _goal_split(conn, t0, t1, pid)
    elif by:
        raise RiverError(f"maxpm health --by {by}: the one split is goal (maxpm health --by goal)")
    return res


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
    ai_active = [a for a in agents if a["kind"] == "ai" and a["state"] == "active"
                 and a.get("role") not in ("planner", "expert", "assistant")]  # these take no queue work (#4955, #5110)
    ai_busy = [a for a in ai_active if a["holds"]]
    ai_idle = [a for a in ai_active if not a["holds"]]
    # Waiting sessions (maxpm wait) take new work by themselves within seconds, and end after wait_max.
    ai_waiting = [a for a in ai_idle if a.get("role") == "waiting"]
    in_progress = [a for a in live if a["status"] in ("in_progress", "held")]

    # Width by depth: how many open items could run at each step if every earlier step finished.
    layers: dict[int, dict] = {}
    for a in live:
        if a["status"] in OPEN_STATES and a["depth"] is not None and not a["backlog"]:
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
    if not ready and any(a["status"] == "open" and not a["backlog"] for a in live) and not in_progress:
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
    sql = ("SELECT i.id, i.title, i.short, i.output, i.closed_at, p.name project, "
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
            "open": sum(a["status"] in OPEN_STATES and not a["backlog"] for a in mine),
            "backlog": sum(a["backlog"] for a in mine),
            "ready": sum(a["ready"] for a in mine),
            "in_progress": sum(a["status"] in ("in_progress", "held") for a in mine),
            "human_waiting": sum(a["ready"] and a["doer"] == "human" for a in mine),
            "blocked": sum(a["status"] == "open" and not a["ready"] and not a["backlog"] for a in mine),
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
                          if a["ready"] and a["doer"] == "human" and not a["project_archived"] and not a["held_for"]],
        "due": [{"id": a["id"], "title": a["title"], "project": a["project"], "due_text": a["due_text"],
                 "due_state": a["due_state"]} for a in sorted(ann.values(), key=lambda a: a["due"] or "")
                if a["due"] and a["status"] in OPEN_STATES and not a["backlog"] and not a["project_archived"]],
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


# The steps of the agent that goes through the whole list with the person (#5103, mark 2026-11-10: 'when i go to the
# needs you session i should be able to spawn an agent to work through the list with me').
LIST_STEPS = """How to go through the list with {person}:
1. Take the first one. Before you write to {person}, run its "read it first" command and read the files, links and
   pictures it names.
2. Write one short message about this one only: what it is, in a few plain lines; the options, each with what it
   changes; your recommendation and why. A decision takes the form below. Show a picture or a page in the chat (in
   Claude Code: SendUserFile). Use plain words, not the queue's own words (lease, claim, reserved, annotate), and
   say what a thing is, not only its number.
""" + "\n".join("   " + line if line else "" for line in DECISION_FORMAT.split("\n")) + """
3. Wait for the answer. Then record it at once, in their words: the "record the result" command (done with the
   answer as its note), or drop it with the reason. When the answer asks for a change, add the work
   (maxpm add "<title>" --found-during <id> --context "<what they said>") and close this one.
   If you can do the step yourself without them, say so; with their yes, take it over:
   maxpm --as <your-agent-name> takeover <id> --note "<how>".
4. Then the next one. Never two in one message. When they want to skip one, leave it open and go on; when they
   want to stop, say in one line what is left.
Ask before anything that uses their accounts, money, or sends something in their name.
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
    items = sorted((a for a in ann.values() if a["ready"] and a["doer"] == "human" and not a["project_archived"]
                    and not a["held_for"]), key=lambda a: a["sort_key"])
    qs = [dict(r) for r in conn.execute(
        "SELECT id, from_agent, body, item_id FROM messages WHERE kind='question' AND state='open' AND to_agent=? "
        "AND held_for IS NULL ORDER BY id",
        (person,))]
    if not items and not qs:
        return f"Nothing in the MaximizePM queue waits on {person} now."
    parts = [f"You are helping {person} work through everything that waits on them in their work queue "
             f"(MaximizePM, the `maxpm` command): {len(items)} item(s) and {len(qs)} question(s), most important "
             f"first. Take them one at a time: finish or park one before you start the next. The steps are at the end."]
    parts += [_item_prompt_section(conn, a, ann, person, n) for n, a in enumerate(items, 1)]
    for n, q in enumerate(qs, len(items) + 1):
        parts.append(f"{n}. Question #{q['id']} from {q['from_agent']}" + (f" about #{q['item_id']}" if q["item_id"] else "")
                     + f": {q['body']}\n   Answer it: maxpm --as {person} answer {q['id']} \"<answer>\"")
    parts.append(LIST_STEPS.format(person=person))
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
    platforms = launch_platforms()
    if not words or words[0] not in platforms:
        raise RiverError(f"launch profile {cmd!r}: the platform is one of {', '.join('@' + p for p in platforms)}")
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
    got = _agent_option(key)
    return got[:2] if got else None


def _check_option(platform, opt, value):
    options = platform_spec(platform)["options"]
    spec = options.get(opt)
    if spec is None:
        raise RiverError(f"{platform} has no option {opt!r}; it has {', '.join(options) or 'none'}")
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
    for p, pl in launch_platforms().items():
        if "model_ids" not in pl["options"]:
            continue
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
    """The id a platform's CLI gets for a model_ladder name: its <prefix>model_ids setting when it has that option,
    else its adapter's DESCRIBE['model_ids'] (#5168), else the name."""
    if not model:
        return model
    a = _adapters().find(platform or "")
    if a is None or a.missing():
        return model
    pl = platform_spec(platform)
    if "model_ids" not in pl["options"]:
        return a.describe["model_ids"].get(model.lower(), model)
    ids = (inline or {}).get("model_ids")
    if ids is None:
        ids = setting(conn, pl["prefix"] + "model_ids", project_id=project_id)
    return parse_model_ids(ids, platform).get(model.lower(), model)


def platform_models(conn, platform, project_id=None, inline=None):
    """The models a platform's sessions run, for the launch dialog and a session's Settings dialog (#4633), one
    source for both: the model_ladder names of its family, each with its note and the id its CLI gets."""
    fam = launch_platforms().get(platform, {}).get("family")
    ladder = parse_ladder(setting(conn, "model_ladder"))
    if fam and fam not in ladder:
        # A family with no ladder (a "multi" agent's provider/model texts, #5169): the adapter's own model list.
        # The adapter's own models (DESCRIBE['models']) come first, marked pick: the launch dialog offers them
        # and finds the rest of a long list by typing (#5238; OpenCode knows about 500).
        a = _adapters().find(platform)
        picks = list(a.describe["models"]) if a else []
        names = list(dict.fromkeys(picks + (a.read_models() if a else [])))
        return [{"name": m, "note": "", "family": fam, "id": a.describe["model_ids"].get(m, m), "pick": m in picks}
                for m in names]
    return [{"name": m, "note": MODEL_NOTES.get(m, ""), "family": fam, "id": model_id(conn, platform, m, project_id, inline)}
            for m in ladder.get(fam, [])]


def model_name_of(models, value):
    """The ladder name of a model a session reports (an id such as gpt-6.1-sol or claude-opus-5-5, or a name), or
    None: its id, else its name, else the name as a word of the id."""
    v = str(value or "").lower()
    for m in models:
        if v in (m["name"], str(m["id"]).lower()):
            return m["name"]
    return next((m["name"] for m in models if re.search(r"(?:^|[-_.])" + re.escape(m["name"]) + r"(?:$|[-_.])", v)), None)


# The system whose shell gets the commands river builds: its quoting, and how the page opens a session (a
# tmux pane; a console window on Windows, where tmux does not run). Tests set it.
PLATFORM = sys.platform


def tmux_path():
    """The tmux program, or None when it is not installed: on PATH, else where Homebrew or MacPorts put it
    (the desktop app starts maxpm serve from the Dock, with a short PATH)."""
    return shutil.which("tmux") or next((p for p in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux",
                                                     "/opt/local/bin/tmux") if os.access(p, os.X_OK)), None)


def tmux_install():
    """The command that installs tmux on this system: every agent starts in a tmux pane (#5105)."""
    return "brew install tmux" if PLATFORM == "darwin" else "sudo apt install tmux (Fedora: sudo dnf install tmux)"


def can_start_agents():
    """Whether this computer can open an agent session: tmux is installed, or it is Windows (a console window)."""
    return PLATFORM == "win32" or bool(tmux_path())


def _shell_quote(s):
    import shlex
    if PLATFORM == "win32":
        return f'"{s}"' if not s or re.search(r'[\s"&|<>^%]', s) else s
    return shlex.quote(s)


def river_dir():
    """The folder of the queue, which an agent's sandbox must let it write (Codex --add-dir)."""
    return str(db_path().expanduser().resolve().parent)


def profile_options(conn, platform, inline=None, project_id=None, chosen=None):
    """A profile's option values: the launch dialog's choice, else the entry's own, else the settings (each option
    of a registered agent is a config key, <prefix><option>, #5169)."""
    pl = platform_spec(platform)
    for k, v in (chosen or {}).items():
        spec = pl["options"].get(k)
        if spec is None or spec["kind"] == "text":
            raise RiverError(f"{pl['label']} has no option {k!r} to choose at launch; it has "
                             f"{', '.join(o for o, sp in pl['options'].items() if sp['kind'] != 'text')}")
        _check_option(platform, k, v)
    def default(k):
        # An adapter with no prefix has no config keys: its spec's default.
        if not pl["prefix"]:
            return pl["options"][k]["default"]
        return setting(conn, pl["prefix"] + k, project_id=project_id)
    return {k: (chosen or {}).get(k, (inline or {}).get(k, default(k))) for k in pl["options"]}


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


def shell_command(words):
    """An adapter's argv as one shell command line: each word quoted for the shell, but a Raw word (the args
    setting, as the person wrote it), which goes in as it is."""
    raw = _adapters().Raw
    return " ".join(w if isinstance(w, raw) else _shell_quote(w) for w in words)


def hook_words(ad, folder=None):
    """The words that load an adapter's state hook or plugin (its install_hooks, which also writes the hook's files
    beside the queue), for an agent whose STATE['state'] is 'hook': without them its session reports no state at all
    (#5214, Grok: --agent <file>). [] for an agent whose state comes from the screen (DETECT), and for one that
    takes MaximizePM's hooks file as a setting (LAUNCH['hooks_file']: Claude Code's --settings, behind its
    activity_hooks setting)."""
    if (ad.state or {}).get("state") != "hook" or ad.launch.get("hooks_file"):
        return []
    return [str(w) for w in ad.install_hooks(folder, report_command()) or ()]


def with_hooks(ad, argv, folder=None):
    """argv with hook_words: words that start with env are an env prefix and go first (OpenCode has no flag for a
    plugin: env OPENCODE_CLI_CONFIG_CONTENT=...; an argv that starts with env itself stays valid after it); other
    words are an option of the CLI and go right after the program (pi -e <file>, copilot --plugin-dir <dir>, grok
    --agent <file>), before a subcommand or the prompt."""
    words = hook_words(ad, folder)
    if not words or not argv:
        return argv
    return words + argv if words[0] == "env" else argv[:1] + words + argv[1:]


def build_command(platform, opts, model=None, effort=None, name=None):
    """The command line that starts a platform's CLI with these options, by its adapter's build_command, with the
    words of its state hook (with_hooks). name: the session's name, for a CLI that takes one (claude --name, and
    the Remote Control session name); Codex has no flag for it. opts may hold the MaximizePM keys of
    agents.LAUNCH_KEYS (fork, resume, hooks_file, ...)."""
    ad = _adapters().find(platform)
    if ad is None or not ad.supports("launch"):
        raise RiverError(f"no command builder for {platform}")
    return shell_command(with_hooks(ad, ad.build_command(name, None, model, effort, opts, opts.get("prompt"))))


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
        return platform_spec(prof[0])["exe"]
    return cmd.split()[0] if cmd.split() else ""


# The agents that had launch profiles when the one-time change _migrate_launch_agents came: only their hand-written
# commands become profiles, so a queue that runs it late gets what it got then. Fixed history, not a list of agents.
_PROFILE_MIGRATION = ("claude", "codex")


def profile_from_command(cmd):
    """An old hand-written command that a profile can build exactly -> (platform, options); else None. Each adapter's
    parse_command of an agent in _PROFILE_MIGRATION reads it: unknown flags, a fixed model, or another --add-dir keep the command custom, so nothing
    is lost."""
    import shlex
    try:
        w = shlex.split(cmd)
    except ValueError:
        return None
    if not w:
        return None
    for mid in _PROFILE_MIGRATION:
        ad = _adapters().find(mid)
        got = ad.parse_command(w) if _startable(ad) else None
        if got:
            return _platform_of(ad), {**got["settings"], "prompt": got["prompt"]}
    return None


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
                pl = platform_spec(platform)
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


def waiting_agent_for(conn, project, item_id=None, model=None):
    """An active session that waits for work in this project (maxpm wait), longest waiting first; with
    item_id, only one whose model the item's limits allow, and for a release review, none that worked on
    the release (release_authors) or held a task before (used_session, #4968); with auto_continue tree, only a
    session whose tree holds the item, or one that held no task (#4994). model (maxpm launch --model): only a
    session that runs that model; else a new session starts on it (#5383: launch --item --model fable gave the item
    to a waiting Opus session). A session that recorded no model gets no item with a min-model of its own."""
    it = _item(conn, item_id) if item_id is not None else None
    if it is not None and it["tier"] == "expert":
        return None  # a new session starts for an expert-tier task; a waiting worker does not get it (#4959)
    ladder = parse_ladder(setting(conn, "model_ladder")) if model or (it is not None and it["min_model"]) else None
    want = (ladder_short(ladder, model) or model) if model else None

    def model_fits(name):
        have = agent_model(conn, name)
        if want:
            return bool(have) and (ladder_short(ladder, have) or have) == want
        return bool(have) or it is None or not it["min_model"]
    authors = release_authors(conn, it["id"]) if it is not None and it["kind"] == "review" else set()
    for r in conn.execute("SELECT name, waiting_in FROM agents WHERE role='waiting' AND kind='ai' "
                          "ORDER BY waiting_since").fetchall():
        a = agent_status(conn, r["name"])
        if a["state"] == "active" and not a["holds"] and project in (r["waiting_in"] or "").split(","):
            if (r["name"] not in authors and (it is None or not _model_refusal(conn, it, r["name"]))
                    and model_fits(r["name"])
                    and (it is None or it["kind"] not in FRESH_KINDS or used_session(conn, r["name"], it["id"]) is None)
                    and (it is None or _in_tree(it["id"], tree_mode(conn, r["name"])))):
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


def used_session(conn, agent, item_id=None):
    """The first task that this agent session held, other than item_id, or None: an item it claimed since it
    last registered (a name used again is a new session), or one it holds now (a give moves a lease to it)."""
    start = conn.execute("SELECT MAX(id) FROM events WHERE item_id IS NULL AND actor=? AND change LIKE 'registered as%'",
                         (agent,)).fetchone()[0] or 0
    r = conn.execute("SELECT item_id FROM events WHERE actor=? AND id>? AND item_id IS NOT NULL AND item_id IS NOT ? "
                     "AND change LIKE 'claimed%' ORDER BY id LIMIT 1", (agent, start, item_id)).fetchone()
    if r is None:
        r = conn.execute("SELECT id item_id FROM items WHERE assignee=? AND id IS NOT ? AND status IN ('in_progress','held') "
                         "ORDER BY id LIMIT 1", (agent, item_id)).fetchone()
    return r["item_id"] if r else None


FRESH_KINDS = ("review", "operational")  # a task of these kinds starts in a new session (#4968, #4973)


CLEANUP_TITLE = "Clean up the list of {person}"


def cleanup_trigger(conn):
    """One pass of maxpm serve (#4973): for each person whose list holds list_cleanup_min open items older than
    list_cleanup_after, add one operational task that cleans it up, unless one was added for that person in the
    last list_cleanup_after. The task sits in the project that holds most of those items. Returns the new ids."""
    after = parse_duration(setting(conn, "list_cleanup_after"))
    if not after.total_seconds():
        return []
    least = int(setting(conn, "list_cleanup_min") or 0)
    cut = iso(now() - after)
    added = []
    for h in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY name").fetchall():
        person = h["name"]
        title = CLEANUP_TITLE.format(person=person)
        if conn.execute("SELECT 1 FROM items WHERE kind='operational' AND title LIKE ? AND (status IN ('open',"
                        "'in_progress','held') OR created_at > ?)", (title + "%", cut)).fetchone():
            continue
        old = conn.execute("SELECT DISTINCT i.id, i.title, i.project_id, i.created_at FROM needs_you n JOIN items i "
                           "ON i.id=n.item_id WHERE n.kind='item' AND n.closed_at IS NULL AND (n.human=? OR n.human IS "
                           "NULL) AND i.status='open' AND i.created_at < ? ORDER BY i.created_at", (person, cut)).fetchall()
        if len(old) < max(least, 1):
            continue
        counts = {}
        for r in old:
            counts[r["project_id"]] = counts.get(r["project_id"], 0) + 1
        pid = max(counts, key=lambda k: (counts[k], -k))
        project = conn.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()["name"]
        ids = " ".join(f"#{r['id']}" for r in old[:150]) + (f" and {len(old) - 150} more" if len(old) > 150 else "")
        context = (f"{person}'s list (maxpm needs-you, as {person}) holds {len(old)} open items older than "
                   f"{setting(conn, 'list_cleanup_after')}: {ids}. For each one, find out whether it is still needed: "
                   f"read maxpm show <id>, its related and linked items, and git log --grep '#<id>' in its project. "
                   f"Done or no longer needed: close it with the reason, maxpm done|drop <id> --note \"<why>\" "
                   f"({person} gets one notice and can undo it). The same work as another item: merge them. Still "
                   f"needed: keep it. Add one item for {person} (--doer human) with the ones you cannot judge, each "
                   f"with your question. Edit no code, and do not ask {person} about each item in chat.")
        res = item_add(conn, project, f"{title}: {len(old)} items older than {setting(conn, 'list_cleanup_after')}",
                       priority=2, doer="ai", actor="maxpm", context=context, goals=[], kind="operational")
        added.append(res["id"])
    return added


def reviewer_refusal(conn, item, agent):
    """Why this agent may not take this release review or operational task, or None: it worked on the release
    (author_refusal), or its session held a task before (used_session). A review and an operational task start in
    a new session, with a clean context (#4968, mark: 'all reviews should start with a clean context reviewer';
    #4973); after it a session may take any work. A person may take any of them."""
    why = author_refusal(conn, item, agent)
    if why or item["kind"] not in FRESH_KINDS or not _is_ai(conn, agent):
        return why
    used = used_session(conn, agent, item["id"])
    if used is None:
        return None
    what = "release review" if item["kind"] == "review" else "operational task"
    return (f"this session held #{used} before; {what} #{item['id']} starts in a new session with a clean "
            f"context (maxpm serve starts one)")


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


def launch_target(conn, project=None, agent=None, item=None, model=None, effort=None, spread=False,
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
                + (", for a person" if a["doer"] == "human" else "")
                + (f", in the backlog: maxpm activate {item} first" if a["backlog"] else
                   ", waits on open items" if not a["ready"] else "")
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
                             f"maxpm config set launch_agents \"...; {platform_spec(top['agent'])['label']}=@{top['agent']}\"")
    plat = _launch_agent_cmd(conn, p["id"], agent, None, effort, options, None, prompt)["platform"]
    if model:
        # The agent's family decides whether the limits apply (#5236): a multi agent's provider/model text has no order.
        ok, why = model_check(parse_ladder(setting(conn, "model_ladder")), model, top["min_model"], top["max_model"],
                              platform_spec(plat)["family"] if plat else None)
        if not ok:
            raise RiverError(f"#{top['id']} {why}; pick another model")
        model_why = "chosen for this launch"
    else:
        # No model named: the item's own (#4961), else the default_model setting (#5346: the manager launched seven
        # items with default_model opus and each pane ran Sonnet, the CLI's own default), of the agent's family, so
        # the session declares it (MAXPM_MODEL) and its limits hold. With neither, the agent's own default runs.
        model, model_why = session_model(conn, _item(conn, top["id"]), platform_spec(plat)["family"] if plat else None)
    name = session_title(top["goals"], top["id"], top["title"])
    cmd = None
    if effort is None:
        # The same for the effort (#5346): the item's own, else default_effort; a level that this agent does not
        # take is left out, and the agent's own default runs.
        effort = top.get("effort") or None
        if effort:
            try:
                cmd = _launch_agent_cmd(conn, p["id"], agent, model, effort, options, name, prompt)
            except RiverError:
                effort = None
    cmd = cmd or _launch_agent_cmd(conn, p["id"], agent, model, effort, options, name, prompt)
    if not model and cmd["adapter_default"]:  # #5284: the adapter names a default model; the item names none
        model, model_why = cmd["model"], f"#{top['id']} names no model: the default model of {platform_spec(plat)['label']}"
    base = fork_base(conn, top, p["path"], model, cmd["platform"])
    if base:  # a worker of the goal starts from the warm base: it reads the goal's context from the cache
        cmd = _launch_agent_cmd(conn, p["id"], agent, model, effort, options, name, prompt, fork_of=base["session_id"])
        cmd["env"] = {**cmd["env"], "MAXPM_FORK_OF": base["goal"]}  # its go briefing says what it is now
    return {"project": p["name"], "path": p["path"], "item": {"id": top["id"], "title": top["title"], "agent": top["agent"]},
            "ready": len(pool), "why": why, "session_title": name, **cmd, "fork_of": base, "model_why": model_why}


def agent_for_type(conn, agent_type, project_id=None):
    """The first launch entry (launch_entries) that runs this agent type (a profile of it, or a custom command of
    its CLI)."""
    for label, cmd in launch_entries(conn, project_id):
        prof = parse_profile(cmd)
        if (prof[0] if prof else _exe_platform(cmd)) == agent_type:
            return label
    return None


def launch_entries(conn, project_id=None):
    """The launch_agents entries, then one implicit profile (label: the adapter's label, command: @<agent type>) for
    each registered agent that MaximizePM can start and that no entry runs (#5168): agent_for.<role>, default_agent,
    agent_rules or an item's --agent can name any registered agent, with no launch_agents entry for it. The first
    entry stays the default of a start that names none."""
    out = parse_launch_agents(setting(conn, "launch_agents", project_id=project_id))
    runs = {(parse_profile(c) or (_exe_platform(c),))[0] for _, c in out}
    labels = {lbl.lower() for lbl, _ in out}
    for t in agent_types():
        lbl = platform_spec(t)["label"]
        if t not in runs and lbl.lower() not in labels:
            out.append((lbl, f"@{t}"))
    return out


def role_agent(conn, role, project_id=None, item_id=None):
    """The launch entry label that a session of this role starts (#5168): for an item, its effective agent
    (_item_models: its own --agent, agent_rules, agent_for.<role>, default_agent); else agent_for.<role>, then
    default_agent, of the most specific scope (item, project, global). None when nothing names one: the first
    launch_agents entry."""
    if role not in AGENT_ROLES:
        raise RiverError(f"role {role!r}: one of {', '.join(AGENT_ROLES)}")
    t = None
    if item_id is not None:
        it = _item(conn, item_id)
        if role != item_role(it):  # the item is taken in another role (an expert for a work item): that role's agent
            it = {**dict(it), "kind": {"reviewer": "review", "deployer": "deploy"}.get(role, "work"),
                  "tier": "expert" if role == "expert" else None}
        t = _item_models(it, _project_name(conn, it["project_id"]), _model_defaults(conn),
                         setting(conn, "model_ladder"))["agent"]
        project_id = it["project_id"] if project_id is None else project_id
    else:  # the most specific scope wins: a project's default_agent comes before a global agent_for.<role>
        scopes = ([f"project:{_project_name(conn, project_id)}"] if project_id is not None else []) + ["global"]
        for sc in scopes:
            rows = {r["key"]: r["value"] for r in conn.execute(
                "SELECT key, value FROM settings WHERE scope=? AND key IN (?, 'default_agent')", (sc, f"agent_for.{role}"))}
            t = rows.get(f"agent_for.{role}") or rows.get("default_agent")
            if t:
                break
    if not t:
        return None
    t = _check_agent_type(conn, t)
    label = agent_for_type(conn, t, project_id)
    if label is None:
        raise RiverError(f"the {role} runs {t}, and no launch entry runs it; add one: maxpm config set launch_agents "
                         f"\"...; {platform_spec(t)['label']}=@{t}\"")
    return label



def _start_next(conn):
    """What Start (work: top item) opens now, and why; None when nothing is ready."""
    try:
        t = launch_target(conn, spread=True)
    except RiverError:
        return None
    return {**t["item"], "project": t["project"], "why": t["why"]}


def _launch_agent_cmd(conn, project_id, agent, model=None, effort=None, options=None, name=None, prompt=None,
                      fork_of=None, autocompact=None):
    """The chosen launch_agents entry as a command: a profile builds it from its options (options: the
    launch dialog's choices) and gives the session its name, a custom command gets {model} and {effort}
    filled in. The session also gets MAXPM_MODEL. prompt (maxpm launch --prompt) follows the profile's
    first prompt; a custom command has no prompt option to put it in. autocompact (tokens, the manager's
    manager_autocompact): claude --autocompact, unless the profile's args already name one."""
    agents = launch_entries(conn, project_id)
    pick = agents[0] if agent is None else next((a for a in agents if a[0] == agent), None)
    if pick is None:
        raise RiverError(f"no agent {agent!r} in launch_agents; known: {', '.join(a for a, _ in agents)}")
    model, first = _check_model_name(model) if model else None, None
    if effort and not re.match(r"^[a-z][a-z0-9_-]{0,31}$", effort):
        raise RiverError(f"effort {effort!r}: a level such as low, medium, high")
    prof = parse_profile(pick[1])
    # The CLI gets its own id for the model (codex -m gpt-6-astra); river keeps the ladder name everywhere else.
    platform = prof[0] if prof else _exe_platform(entry_exe(pick[1]))
    # No model named: the adapter's own default (DESCRIBE['default_model'], #5284: OpenCode runs on its free model,
    # not on whatever model its TUI remembers); "" lets the agent pick. The default_model setting is not forced on
    # the CLI (#4961).
    adapter_default = None
    if not model and platform:
        a = _adapters().find(platform)
        adapter_default = (a.describe.get("default_model") or None) if a else None
        model = _check_model_name(adapter_default) if adapter_default else None
    mid = model_id(conn, platform, model, project_id, prof[1] if prof else None)
    if effort and platform:
        _check_agent_effort(_adapters().get(platform), mid, effort)
    prompt = custom_prompt(prompt) if prompt is not None else None
    if prof:
        opts = profile_options(conn, prof[0], prof[1], project_id, options)
        if prompt:
            opts = {**opts, "prompt": join_prompt(opts["prompt"], prompt)}
        # What MaximizePM adds to the start, each only where the adapter takes it (agents.LAUNCH_KEYS): the skill in
        # the system prompt, a fork of a goal's warm base (fork_base), the hooks file, the manager's autocompact.
        caps, built = _adapters().get(prof[0]).launch, dict(opts)
        if caps.get("system_prompt_file") and opts.get("skill_prompt") == "on":
            built["system_prompt_file"] = True
        if fork_of and caps.get("fork"):
            built["fork"] = fork_of
        if caps.get("hooks_file") and opts.get("activity_hooks") == "on":
            built["hooks_file"] = hook_file(_adapters().get(prof[0]))
        if autocompact and caps.get("autocompact") and "--autocompact" not in opts.get("args", ""):
            built["autocompact"] = autocompact
        else:
            autocompact = None
        cmd = build_command(prof[0], built, mid, effort or None, name)
        session_id = _adapters().get(prof[0]).start_session(name, built)  # a start that names its session (#5280)
        if _adapters().get(prof[0]).first_prompt == "paste":  # not on its command line: serve pastes it (#5221)
            first = opts.get("prompt")
    elif prompt:
        raise RiverError(f"{pick[0]} is a custom command ({pick[1]}), so it takes no --prompt; give it a profile "
                         f"in launch_agents (@claude-code or @codex) for one")
    elif options:
        raise RiverError(f"{pick[0]} is a custom command ({pick[1]}), so it has no launch options; "
                         f"give it a profile in launch_agents (@claude-code or @codex) for them")
    else:
        opts, cmd, autocompact = {}, fill_launch_command(pick[1], mid, effort or None, name), None
    if not prof:
        session_id = None
    return {"agent": pick[0], "command": cmd, "platform": prof[0] if prof else None, "options": opts,
            "session_id": session_id,
            "model": model, "model_id": mid, "effort": effort or None, "custom_prompt": prompt,
            "adapter_default": bool(adapter_default),
            "autocompact": autocompact, "first_prompt": first,
            "env": {"MAXPM_MODEL": model} if model else {}}


# The first prompt of a session that maxpm serve moved into a tmux pane (tmux_adopt): it resumes the same chat.
ADOPT_PROMPT = ("MaximizePM moved this session into a tmux pane, so the Sessions page can type into it; your name, items "
                "and context are the same. Start again any background command you had running (for example maxpm "
                "manage --watch, or maxpm inbox --wait), then continue where you stopped: run maxpm --as {agent} go "
                "if you are not sure.")


def _ran_model(pid_cmd, platform, session_id, transcript=None):
    """The model a session ran on, as its CLI named it, when the queue has none for it: the --model (-m) of its
    recorded command line, else the model of the last reply in its chat log (the adapter of the platform finds it
    by the session id when its CHAT['ref'] is 'session', and reads it, #5182). None when neither says."""
    m = re.search(r"(?:^|\s)(?:--model|-m)[ =]([A-Za-z0-9._:/\[\]-]+)", pid_cmd or "")
    if m:
        return m.group(1)
    from . import agents, sessions
    ad = agents.find(platform)
    path = transcript
    if not path and ad is not None and ad.chat_ref() == "session" and session_id:
        path = ad.chat_path(session_id)
    if ad is None or not path or not Path(path).exists():
        return None
    try:
        return sessions._transcript_settings(ad.id, Path(path)).get("model")
    except (OSError, ValueError):
        return None


def command_settings(ad, cmd):
    """{key: value} of the adapter's launch settings that a running command line shows (#5274): a choice or a text by
    its "read" rule (group 1), a toggle by its flag (on when the flag's first word is on the line, else off). A
    setting with no flag and no rule (built by build_command in its own way), and the prompt and args, are not read:
    the line does not say them."""
    import shlex
    try:
        words = shlex.split(cmd or "")
    except ValueError:
        words = (cmd or "").split()
    out = {}
    for spec in (ad.settings if ad else ()):
        key = spec["key"]
        if key in ("prompt", "args", "model_ids"):
            continue
        if spec["kind"] == "toggle":
            if spec.get("flag"):
                out[key] = "on" if spec["flag"][0] in words else "off"
            continue
        if spec.get("read"):
            m = re.search(spec["read"], cmd or "")
            if m:
                out[key] = m.group(1)
    return out


def resume_command(conn, agent, platform, session_id, project_id=None, overrides=None, transcript=None):
    """The command that resumes an agent's chat in a new process (tmux_adopt, #4598): the first launch_agents
    profile of its platform, its model, `claude --resume <session id>` or `codex resume <thread id>`, and
    ADOPT_PROMPT as its first prompt. {command, env}. overrides (the Settings dialog, #4625): model (a model_ladder
    name or the CLI's own id), effort, opts (profile options such as permission_mode or sandbox), flags (added),
    noflags (taken out of the profile's args), prompt. The chat resumes on the model it ran on: agents.model, else
    the model its command line or its transcript names (transcript: the file, when the caller has it). With none,
    it refuses: a command with no model gets the CLI's default (#5064: the manager came back on Fable)."""
    ov = overrides or {}
    a = _agent(conn, agent)
    agents = launch_entries(conn, project_id)
    pick = next((x for x in agents if (parse_profile(x[1]) or (None,))[0] == platform), None)
    if pick is None:
        raise RiverError(f"no {platform} profile in launch_agents (@{platform}): MaximizePM cannot resume {agent}")
    prof = parse_profile(pick[1])
    try:
        model = _check_model_name(a["model"]) if a["model"] else None
    except RiverError:
        model = None
    if ov.get("model"):
        try:
            model = _check_model_name(ov["model"])
        except RiverError:
            model = None
    mid = model_id(conn, platform, model, project_id, prof[1])
    if ov.get("model") and not model:
        mid = ov["model"]  # the CLI's own id
    if not mid:
        seen = _ran_model(a["pid_cmd"], platform, session_id, transcript)
        if seen:
            model = model_name_of(platform_models(conn, platform, project_id, prof[1]), seen)
            mid = model_id(conn, platform, model, project_id, prof[1]) if model else seen
    if not mid:
        raise RiverError(f"MaximizePM does not know the model {agent} ran on (not in the queue, its command line or "
                         f"its transcript), and a resume with no model takes the CLI's default: nothing resumed. "
                         f"The session records it with maxpm --as {agent} go --model <model>; or pick the model in "
                         f"its Settings on the Sessions tab")
    opts = {**profile_options(conn, prof[0], prof[1], project_id, None),
            "prompt": ov.get("prompt") or ADOPT_PROMPT.format(agent=agent), **(ov.get("opts") or {})}
    args = " ".join(x for x in opts.get("args", "").split() if x not in (ov.get("noflags") or ()))
    opts["args"] = " ".join([args] + [f for f in (ov.get("flags") or ()) if f not in args.split()]).strip()
    ad = _adapters().get(platform)
    # The effort it ran at: from its recorded command line, by the adapter's rule (the agents table keeps no effort).
    m = re.search(ad.effort["read"], a["pid_cmd"] or "") if (ad.effort or {}).get("read") else None
    effort = ov.get("effort") or (m.group(1) if m else None)
    settings = {**opts, "noflags": list(ov.get("noflags") or ())}
    if ad.launch.get("hooks_file") and opts.get("activity_hooks") == "on":
        settings["hooks_file"] = hook_file(ad)
    tokens = autocompact_tokens(setting(conn, "manager_autocompact")) if a["role"] == "manager" else None
    if tokens and ad.launch.get("autocompact"):
        settings["autocompact"] = tokens
    argv = ad.resume_command(session_id, mid, effort, settings, name=a["session"] or agent)
    if not argv:
        raise RiverError(f"{ad.label} cannot resume a session: MaximizePM cannot resume {agent}")
    cmd = shell_command(with_hooks(ad, argv))
    return {"command": cmd, "agent": pick[0], "platform": platform,
            # An agent that takes no prompt on its command line gets it pasted into its pane by serve (#5221).
            "first_prompt": settings.get("prompt") if ad.first_prompt == "paste" else None,
            "env": {"MAXPM_AGENT": agent, **({"MAXPM_MODEL": model} if model else {})}}


def _check_agent_effort(ad, mid, effort):
    """Refuse an effort that an agent with a per-model reader of its levels (EFFORT and read_effort_levels: Codex)
    does not take: for the model, when the reader lists it, else the adapter's EFFORT levels. An agent with no such
    reader takes the effort as it is."""
    read = getattr(ad.module, "read_effort_levels", None)
    if not read or not ad.effort:
        return
    levels = read(mid) or ad.effort.get("levels") or []
    if effort not in levels:
        raise RiverError(f"{ad.label}{' ' + mid if mid else ''} takes effort {', '.join(levels)}, not {effort}; "
                         f"pick one of them")


def _platform_entry(describe, specs):
    """A launch platform from an adapter's DESCRIBE and its setting specs (Adapter.options()): {label, exe, family,
    prefix, options: {key: {kind, default, choices, text, label}}}."""
    return {"label": describe["label"], "exe": describe["exe"], "family": describe["family"],
            "prefix": describe.get("prefix", ""),
            "options": {k: {"kind": sp["kind"], "default": sp["default"], "choices": list(sp["choices"]),
                            "text": sp["text"], "label": sp["label"]} for k, sp in specs.items()}}


# Each option of a built-in agent is also a setting, <prefix><option>, so a project can differ (most specific wins):
# its default goes in DEFAULT_SETTINGS here, once, so setting() finds it without a walk of the registry. A user's
# adapter module adds its defaults through setting_default (#5169). No table of platforms (#5185).
from . import agents as _agents_pkg  # noqa: E402
for _mid in _agents_pkg.BUILTIN:
    _ad = _agents_pkg.Adapter(_agents_pkg._builtin(_mid), "builtin")
    for _o, _spec in _ad.options().items():
        DEFAULT_SETTINGS.setdefault(_ad.describe["prefix"] + _o, _spec["default"])


def launch_platforms():
    """{platform: entry} of every registered agent that MaximizePM can start (it has its launch part): the built-in
    ones under their platform names (claude-code, codex), the others under their id, each with the options of its
    setting specs (platform_spec, #5169)."""
    return {_platform_of(a): platform_spec(_platform_of(a)) for a in _adapters().load().values() if not a.missing()}


def _agent_option(key):
    """(platform, option, entry) when `key` is the config key <prefix><option> of a registered agent's setting, or
    None."""
    for name, pl in launch_platforms().items():
        if pl["prefix"] and key.startswith(pl["prefix"]) and key[len(pl["prefix"]):] in pl["options"]:
            return name, key[len(pl["prefix"]):], pl
    return None


def setting_default(key):
    """The built-in value of setting `key`: DEFAULT_SETTINGS, else the default of a registered agent's setting (an
    adapter that is not built in, #5169); RiverError when no setting has that name."""
    if key in DEFAULT_SETTINGS:
        return DEFAULT_SETTINGS[key]
    got = _agent_option(key)
    if got:
        return got[2]["options"][got[1]]["default"]
    raise RiverError(f"unknown setting {key!r}; known: {', '.join(sorted(DEFAULT_SETTINGS))}")


def agent_setting_defaults():
    """{config key: default} of every registered agent's settings, the built-in ones included."""
    return {pl["prefix"] + k: o["default"] for pl in launch_platforms().values() for k, o in pl["options"].items()}
# One line per model in the launch dialog: when it fits.
MODEL_NOTES = {
    "haiku": "quick, simple work: lookups, formatting, trivial edits",
    "sonnet": "routine, well specified work: checks, small fixes, operational tasks",
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
    """What the launch dialog offers for each launch entry (launch_entries: the launch_agents entries, then each
    registered agent that no entry runs, #5234): its family, models, and effort levels."""
    ladder = parse_ladder(setting(conn, "model_ladder"))
    levels = _levels(setting(conn, "effort_levels"))
    out = []
    for label, cmd in launch_entries(conn):
        prof = parse_profile(cmd)
        ad = _adapters().find(prof[0]) if prof else None
        # A custom command: the agent whose program it runs (its first word), for the family and the effort levels.
        by_exe = None if prof else _adapters().by_exe(entry_exe(cmd))
        fam = platform_spec(prof[0])["family"] if prof else by_exe and by_exe.describe["family"]
        fam = fam if fam in ladder else None
        out.append({"label": label, "family": fam, "platform": prof[0] if prof else None,
                    "agent_type": prof[0] if prof else _exe_platform(cmd),
                    "models": (platform_models(conn, prof[0], inline=prof[1]) if prof else
                               [{"name": m, "note": MODEL_NOTES.get(m, ""), "family": f}
                                for f, ms in ladder.items() if fam in (None, f) for m in ms]),
                    # A profile offers the efforts of its adapter (#5169); none when the agent has no effort.
                    "efforts": ad.effort_choices() if ad else (
                        list((by_exe.effort or {}).get("levels") or levels) if fam and by_exe else levels),
                    "takes_model": bool(prof) or "{model}" in cmd,
                    "takes_effort": bool(ad.effort) if ad else "{effort}" in cmd,
                    # The options the dialog can change for one launch, preset from the entry and the settings.
                    "options": profile_option_list(conn, prof[0], prof[1], kinds=("toggle", "choice")) if prof else []})
    return out


def profile_option_list(conn, platform, inline=None, project_id=None, kinds=("toggle", "choice", "text")):
    """A platform's options with their values, for the page: [{name, label, setting, kind, choices, value, text}]."""
    pl = platform_spec(platform)
    vals = profile_options(conn, platform, inline, project_id)
    return [{"name": k, "label": sp["label"], "setting": pl["prefix"] + k, "kind": sp["kind"], "choices": sp.get("choices", []),
             "value": vals[k], "text": sp["text"], "fixed": k in (inline or {})}
            for k, sp in pl["options"].items() if sp["kind"] in kinds]


def launch_profiles(conn):
    """Settings > Setup: each platform that a launch_agents entry uses, with all its options."""
    used = []
    for _, cmd in parse_launch_agents(setting(conn, "launch_agents")):
        prof = parse_profile(cmd)
        if prof and prof[0] not in used:
            used.append(prof[0])
    return [{"platform": p, "label": platform_spec(p)["label"], "options": profile_option_list(conn, p)}
            for p in used]


def recent_events(conn, limit=40):
    return [dict(r) for r in conn.execute(
        "SELECT e.id, e.at, e.actor, e.change, e.item_id, i.title FROM events e LEFT JOIN items i ON i.id=e.item_id "
        "ORDER BY e.id DESC LIMIT ?", (limit,))]


def state(conn, closed=True):
    """Everything the web page shows, in one read.

    closed: False leaves done and dropped items out (the page asks for them only while a view shows them, #4709);
    closed_counts then still says how many each project has."""
    ann = annotate(conn)
    items = sorted(ann.values(), key=lambda a: a["sort_key"])
    shown = [a for a in items if not a["project_archived"]]
    closed_counts = {}
    for a in shown:
        if a["status"] in CLOSED_STATES:
            closed_counts[a["project"]] = closed_counts.get(a["project"], 0) + 1
    return {
        "now": iso(now()),
        "projects": project_list(conn),
        "archived_projects": project_archived(conn),  # the Plan tab's Archived list (#5197)
        "items": [{k: v for k, v in a.items() if k != "sort_key"} for a in shown
                  if closed or a["status"] not in CLOSED_STATES],
        "closed_counts": closed_counts,
        "agents": [agent_status(conn, r["name"]) for r in conn.execute("SELECT name FROM agents ORDER BY name")],
        "capacity": capacity(conn, ann),
        "targets": targets_view(conn, ann),
        "hook_events": dict(HOOK_EVENTS),  # the Targets tab offers a hook for each event
        # The Start buttons' agents: every launch entry, so each registered agent can start from the page (#5234).
        "launch_agents": [label for label, _ in launch_entries(conn)],
        "model_ladder": parse_ladder(setting(conn, "model_ladder")),
        "model_ids": model_ids(conn),
        "launch_options": launch_options(conn),
        "tmux": can_start_agents(),  # every agent starts in tmux (Windows: a console window): the page says when it is missing
        "tmux_install": tmux_install(),
        "start_next": _start_next(conn),
        "queues": {r["agent"]: queue_list(conn, r["agent"], ann)["entries"]
                   for r in conn.execute("SELECT DISTINCT agent FROM queue_entries ORDER BY agent")},
        "manager": manager_view(conn),
        "effort_levels": _levels(setting(conn, "effort_levels")),
        "settings": config_list(conn),
        "events": recent_events(conn),
        "canvases": canvas_heads(conn),  # the page reloads an open canvas when its version changes (#4768)
        "takeovers": takeovers(conn),
        "policies": policy_list(conn),  # the Settings tab's Policies list (#5308)
        "policy_roles": list(POLICY_ROLES),
        "needs_you": [dict(r) for r in conn.execute(
            "SELECT id, kind, item_id, message_id, human, summary, opened_at FROM needs_you "
            "WHERE closed_at IS NULL ORDER BY id DESC")],
    }


# ---------------------------------------------------------------- policies (#5308)
# A policy is a rule that sessions follow, with its source and reason, kept by MaximizePM instead of in one agent's
# memory files: Claude Code memory reaches only Claude Code sessions in one folder, and nobody sees which rule applied.
# maxpm go, manage and the expert briefing print the policies that apply to the session. No rule engine: a policy
# that must be enforced becomes code or a setting.
#
# Two kinds (mark 2026-11-11). GLOBAL policies ship with MaximizePM in river/policies.json: they apply to every
# user's queue and change only by a commit and a release (ids g1, g2, ...; scope the queue or a role, never a
# project, which is a user's own). USER policies are the rules a person sets for their own queue: rows of the
# policies table (ids p1, p2, ...), which never go into the repo. maxpm policy add, edit and retire change user
# policies only; a user policy may add to a global one (adds_to).

GLOBAL_POLICIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "policies.json")

# The roles a policy can name. A worker policy applies to every session that takes queue work: a worker, a goal
# owner, a reviewer, a deployer (they are workers with a kind of task). A manager or an expert gets only its own.
POLICY_ROLES = ("worker", "reviewer", "deployer", "owner", "manager", "expert")


def _policy_roles(role):
    """The policy roles that apply to a session with this briefing role."""
    if role in ("manager", "expert"):
        return {role}
    if role == "assistant":
        return set()  # the assistant of a person takes no queue work: only the queue and project policies
    return {"worker"} | ({role} if role in POLICY_ROLES else set())


def global_policies():
    """The global policies that ship with MaximizePM, from river/policies.json, as the views of user policies are."""
    import json
    try:
        with open(GLOBAL_POLICIES_FILE, encoding="utf-8") as f:
            rows = json.load(f).get("policies", [])
    except FileNotFoundError:
        return []
    out = []
    for r in rows:
        roles = _global_roles(r.get("role"))
        out.append({"id": r["id"], "pid": r["id"], "kind": "global", "project": None, "folder": None,
                    "role": ", ".join(roles) or None, "roles": roles,
                    "text": r["text"], "reason": r["reason"], "source": r["source"], "items": [], "adds_to": None,
                    "scope": "global: " + (", ".join(roles) or "queue"), "created_by": None, "created_at": None,
                    "changed_at": None})
    return out


def _global_roles(role):
    """The roles of a global policy: its "role" is none (every session), one role, or a list of roles."""
    if not role:
        return []
    return [role] if isinstance(role, str) else list(role)


def _global_policy(gid):
    g = {p["id"]: p for p in global_policies()}.get(str(gid).strip().lower())
    if not g:
        raise RiverError(f"no global policy {gid!r}; the global ones: maxpm policy list --global")
    return g


def _is_global_id(pid):
    t = str(pid).strip().lower()
    return t[:1] == "g" and t[1:].isdigit()


def _policy_id(pid):
    if _is_global_id(pid):
        raise RiverError(f"{str(pid).strip().lower()} is a global policy: it ships with MaximizePM "
                         f"(river/policies.json) and changes only by a commit and a release. Add your own rule to it: "
                         f"maxpm policy add \"<rule>\" --reason \"<why>\" --adds-to {str(pid).strip().lower()}")
    s = str(pid).strip().lower().lstrip("p")
    if not s.isdigit():
        raise RiverError(f"no policy {pid!r}; a policy id is p12 (yours) or g3 (global): maxpm policy list")
    return int(s)


def _policy_row(conn, pid):
    r = conn.execute("SELECT x.*, p.name project FROM policies x LEFT JOIN projects p ON p.id=x.project_id "
                     "WHERE x.id=?", (_policy_id(pid),)).fetchone()
    if not r:
        raise RiverError(f"no policy p{_policy_id(pid)}; list them: maxpm policy list")
    return r


def _policy_role(role):
    role = (role or "").strip().lower() or None
    if role in (None, "any"):
        return None
    if role not in POLICY_ROLES:
        raise RiverError(f"no policy role {role!r}; roles: {', '.join(POLICY_ROLES)} (or any)")
    return role


def _policy_items(conn, items):
    """Item ids as the policy keeps them: each must exist."""
    ids = []
    for x in items or []:
        for part in str(x).replace(",", " ").split():
            i = int(part.lstrip("#")) if part.lstrip("#").isdigit() else None
            if i is None or not conn.execute("SELECT 1 FROM items WHERE id=?", (i,)).fetchone():
                raise RiverError(f"no item {part}; link a policy to items that exist: --item <id>")
            if i not in ids:
                ids.append(i)
    return " ".join(map(str, ids)) or None


def _policy_folder(path):
    """A policy's folder as it is kept: absolute, no link, no slash at the end; it must exist (#5376)."""
    f = os.path.realpath(os.path.expanduser(str(path).strip()))
    if not str(path).strip() or not os.path.isdir(f):
        raise RiverError(f"no folder {path!r}; a policy names a folder that exists: --folder ~/Projects/<name>")
    return _norm_path(f).rstrip("/") or "/"


def _show_folder(f):
    home = _norm_path(os.path.expanduser("~")).rstrip("/")
    return "~" + f[len(home):] if f == home or f.startswith(home + "/") else f


def _in_folder(path, folder):
    """True when path is the folder or a path inside it."""
    p = _norm_path(os.path.realpath(os.path.expanduser(path))).rstrip("/")
    return p == folder or p.startswith(folder.rstrip("/") + "/")


def policy_scope(p):
    """The scope of a user policy in words: user: queue, the project or the folder, the role, or both."""
    where = p.get("project") or ("folder " + _show_folder(p["folder"]) if p.get("folder") else None)
    return "user: " + (", ".join(x for x in (where, p.get("role")) if x) or "queue")


def _policy_view(conn, r):
    p = dict(r)
    p.pop("project_id", None)
    p["pid"] = f"p{p['id']}"
    p["kind"] = "user"
    p["roles"] = [p["role"]] if p["role"] else []
    p["scope"] = policy_scope(p)
    ids = [int(i) for i in (p.get("items") or "").split()]
    p["items"] = [{"id": i, "title": x["title"], "status": x["status"]} for i in ids
                  for x in [conn.execute("SELECT title, status FROM items WHERE id=?", (i,)).fetchone()] if x]
    return p


def _adds_to(gid):
    return _global_policy(gid)["id"] if gid else None


def policy_add(conn, text, reason, source=None, project=None, role=None, items=None, actor=None, adds_to=None,
               folder=None):
    """Add a user policy. source says who decided it and when; with none, the actor and today's date. adds_to: the
    global policy (g<n>) it adds to. folder: every session whose folder (or its projects' folder) is in it, for the
    projects that share one folder (#5376); a policy names a project or a folder, not both."""
    text, reason = (text or "").strip(), (reason or "").strip()
    if not text:
        raise RiverError("say the rule: maxpm policy add \"<rule>\" --reason \"<why>\" --source \"<who, date>\"")
    if not reason:
        raise RiverError("say why the rule exists, so a session can judge a case it does not name: "
                         "maxpm policy add \"<rule>\" --reason \"<why>\"")
    if project and folder:
        raise RiverError("a policy names a project or a folder, not both: --project <name> or --folder <path>")
    pid = _project(conn, project)["id"] if project else None
    folder = _policy_folder(folder) if folder else None
    role = _policy_role(role)
    source = (source or "").strip() or f"{actor or 'unknown'} {now().date().isoformat()}"
    adds_to = _adds_to(adds_to)
    with tx(conn):
        cur = conn.execute("INSERT INTO policies(project_id, folder, role, text, source, reason, items, adds_to, "
                           "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (pid, folder, role, text, source, reason, _policy_items(conn, items), adds_to, actor,
                            iso(now())))
        p = _policy_view(conn, _policy_row(conn, cur.lastrowid))
        _event(conn, None, actor, f"policy {p['pid']} added ({p['scope']}): {text}")
    return p


def policy_edit(conn, pid, text=None, reason=None, source=None, project=None, no_project=False, role=None,
                items=None, actor=None, adds_to=None, folder=None, no_folder=False):
    """Change a policy: its text, reason, source, scope (project or folder, role; role any clears it), or its item
    links (items replaces them; an empty list clears them). A project replaces the folder, and a folder the
    project."""
    r = _policy_row(conn, pid)
    ch = {}
    for k, v in (("text", text), ("reason", reason), ("source", source)):
        if v is not None:
            if not v.strip():
                raise RiverError(f"the {k} of a policy cannot be empty; to remove the policy: maxpm policy retire p{r['id']}")
            ch[k] = v.strip()
    if project and no_project:
        raise RiverError("give --project <name> or --no-project, not both")
    if folder and no_folder:
        raise RiverError("give --folder <path> or --no-folder, not both")
    if project and folder:
        raise RiverError("a policy names a project or a folder, not both: --project <name> or --folder <path>")
    if project:
        ch["project_id"] = _project(conn, project)["id"]
        ch["folder"] = None
    elif no_project:
        ch["project_id"] = None
    if folder:
        ch["folder"] = _policy_folder(folder)
        ch["project_id"] = None
    elif no_folder:
        ch["folder"] = None
    if role is not None:
        ch["role"] = _policy_role(role)
    if items is not None:
        ch["items"] = _policy_items(conn, items)
    if adds_to is not None:  # "" or "none": it adds to no global policy
        ch["adds_to"] = None if adds_to.strip().lower() in ("", "none") else _adds_to(adds_to)
    if not ch:
        raise RiverError(f"say what changes: maxpm policy edit p{r['id']} --text|--reason|--source|--project|"
                         f"--no-project|--folder|--no-folder|--role|--item|--adds-to")
    with tx(conn):
        conn.execute(f"UPDATE policies SET {', '.join(k + '=?' for k in ch)}, changed_at=? WHERE id=?",
                     (*ch.values(), iso(now()), r["id"]))
        p = _policy_view(conn, _policy_row(conn, r["id"]))
        _event(conn, None, actor, f"policy {p['pid']} changed ({', '.join(k.replace('_id', '') for k in ch)})")
    return p


def policy_retire(conn, pid, actor=None):
    """Delete a policy. No tombstone: the history keeps one line with its text."""
    p = _policy_view(conn, _policy_row(conn, pid))
    with tx(conn):
        conn.execute("DELETE FROM policies WHERE id=?", (p["id"],))
        _event(conn, None, actor, f"policy {p['pid']} retired ({p['scope']}): {p['text']}")
    return p


def policy_show(conn, pid):
    if _is_global_id(pid):
        g = _global_policy(pid)
        return dict(g, added_by_user=[p for p in policy_list(conn, kind="user") if p["adds_to"] == g["id"]])
    return _policy_view(conn, _policy_row(conn, pid))


def policy_list(conn, project=None, role=None, kind=None):
    """Every policy, global ones first, or those of one project or role (a filter on the scope as written, not on
    what applies). kind: global or user for one kind. A project filter leaves out the global ones: they name none."""
    out = []
    if kind != "user" and not project:
        out = [g for g in global_policies() if not role or _policy_role(role) in g["roles"]]
    if kind == "global":
        return out
    q, args = [], []
    if project:
        q.append("x.project_id=?")
        args.append(_project(conn, project)["id"])
    if role:
        q.append("x.role IS ?")
        args.append(_policy_role(role))
    rows = conn.execute("SELECT x.*, p.name project FROM policies x LEFT JOIN projects p ON p.id=x.project_id "
                        + ("WHERE " + " AND ".join(q) if q else "")
                        + " ORDER BY x.project_id IS NOT NULL OR x.folder IS NOT NULL, x.folder, p.rank, p.name, "
                          "x.role IS NOT NULL, x.role, x.id", args)
    return out + [_policy_view(conn, r) for r in rows]


def policies_for(conn, projects=(), role=None, cwd=None):
    """The policies that apply to a session, global ones first: the queue's, those of its projects and its folders
    (cwd and the folders of its projects), and those of its role (also in its projects). A manager runs every
    project: it gets the queue's and every manager policy, and the count of the project policies it does not see."""
    roles = _policy_roles(role)
    folders = [cwd] if cwd else []
    for name in projects:
        r = conn.execute("SELECT path FROM projects WHERE name=?", (name,)).fetchone()
        if r and r["path"]:
            folders.append(r["path"])
    out = []
    for p in policy_list(conn):
        if role == "manager":
            hit = "manager" in p["roles"] or (not p["roles"] and not p["project"] and not p.get("folder"))
        else:
            where = ((not p["project"] or p["project"] in projects)
                     and (not p.get("folder") or any(_in_folder(f, p["folder"]) for f in folders)))
            hit = where and (not p["roles"] or bool(roles & set(p["roles"])))
        if hit:
            out.append(p)
    return out


# ---------------------------------------------------------------- go

ROLES = ("deployer", "reviewer", "owner", "worker", "unblocker", "manager", "context", "expert", "assistant", "idle")


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
                            "assignee": a["assignee"], "doer": a["doer"], "backlog_blockers": a["backlog_blockers"]}
                           for a in open_items],
            "items_backlog": [{"id": i, "title": ann[i]["title"]} for i in g["items_backlog"]],
            "items_done": len(g["items_done"]), "blockers_held": blockers,
            "lease": _short(_goal_lease(conn, _goal(conn, name), actor)),
            "handoff": goal_handoff(conn, name)["handoff"], "handoff_due": g["handoff_due"],
            "parent_handoff": parent_handoff(conn, _goal(conn, name))}


def go(conn, cwd, actor=None, project=None, role=None, session=None, focus=None, model=None, chat=False,
       agent_type=None):
    """One call for a fresh agent session; see _go. The briefing ends with the policies for the session (#5308)."""
    brief = _go_brief(conn, cwd, actor, project, role, session, focus, model, chat, agent_type)
    if brief.get("agent") and brief.get("role"):
        it = brief.get("item")
        names = list(brief.get("projects") or [])
        if it and it.get("project") and it["project"] not in names:
            names.append(it["project"])
        brief["policies"] = policies_for(conn, names, brief["role"], cwd)
    return brief


def _go_brief(conn, cwd, actor=None, project=None, role=None, session=None, focus=None, model=None, chat=False,
              agent_type=None):
    brief = _go(conn, cwd, actor, project, role, session, focus, model, chat, agent_type)
    brief["asked_open"] = asked_open(conn, brief.get("agent"))  # #5001: the asker closes what is answered or moot
    it = brief.get("item")
    if it and it.get("tier") == "expert" and _agent(conn, brief["agent"])["role"] == "expert":
        # It took an expert-tier task (launched for it): from now on it is an expert session (#4959).
        brief.update(role="expert", item=None, holds=[it], expert=expert_view(conn, _agent(conn, brief["agent"])),
                     why=f"you took the expert-tier task #{it['id']}: you are an expert session, and the person "
                         f"talks it through with you")
        return brief
    if it and it["kind"] == "work":
        # Work like this item, open or done before, so the worker looks before it repeats it (#2257).
        brief["related"] = related(conn, it["id"])
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
            for h in (parent_handoff(conn, r), _handoff(conn, r["id"]) and dict(
                    _handoff(conn, r["id"]), goal=r["name"], due=_handoff_due(conn, r) if r["shared"] else None)):
                if h and h["goal"] not in seen:
                    seen.add(h["goal"])
                    brief["handoffs"].append(h)
        # The item's canvases (#4769): the pages a session made for it, and the comments that wait for a change.
        brief["canvases"] = [{"name": x["name"], "version": x["version"], "open_comments": x["open_comments"]}
                             for x in canvas_heads(conn) if x["item_id"] == it["id"]]
        # Items of a shared goal have no owner who plans them: the agent tags the items it adds itself.
        brief["shared_goals"] = [r["name"] for r in conn.execute(
            "SELECT g.name FROM item_goals ig JOIN goals g ON g.id=ig.goal_id WHERE ig.item_id=? AND g.shared=1 "
            "AND g.status='open' ORDER BY g.rank, g.id", (it["id"],))]
    if "messages" in brief:
        # Counted after the claim: it accepts the push alert of the item this briefing gives, so the alert is
        # not unread any more. The count from before sent each started session to an empty inbox (#1605).
        brief["messages"] = unread(conn, brief["agent"])
    if not it and brief.get("agent"):
        # Claude Code holds a typed message (the terminal, the page's message box, SendMessage) while a foreground
        # command runs: a session there waits in a background command, so a talk reaches it at once (#4948).
        kind = agent_type or session_type(conn, brief["agent"])
        ad = _adapters().find(kind) if kind else None
        brief["wait_background"] = bool(ad and ad.launch.get("background"))  # its CLI runs maxpm wait in the background
    return brief


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
                raise RiverError("no projects in this queue yet: add one: maxpm project add <name>")
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
    open_in_area = [a for a in in_area if a["status"] in OPEN_STATES and not a["backlog"]]
    human_ready = sorted((a for a in in_area if a["ready"] and a["doer"] == "human"), key=lambda a: a["sort_key"])
    brief = {"agent": actor, "new_name": new_name, "projects": names, "descriptions": descs, "trackers": trackers,
             "human_waiting": [{"id": a["id"], "title": a["title"],
                                "blocks": [{"id": d, "title": ann[d]["title"], "priority": ann[d]["effective_priority"]}
                                           for d in a["unblocks"] if ann[d]["status"] in OPEN_STATES]}
                               for a in human_ready],
             "has_history": bool(history(conn, actor, limit=1)),
             "auto_continue": setting(conn, "auto_continue", agent=actor) in ("on", "tree"),
             "continue_mode": setting(conn, "auto_continue", agent=actor),
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
    me = _agent(conn, actor)
    if me["role"] == "expert" and role in (None, "expert"):
        # An expert takes no queue work by itself: it answers the person (#4955).
        brief.update(role="expert", item=None, expert=expert_view(conn, me),
                     holds=[item_show(conn, r["id"]) for r in conn.execute(
                         "SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held') ORDER BY id",
                         (actor,))],
                     why="you are an expert session: you answer the person and take no queue work by yourself")
        return brief
    if me["role"] == "assistant":
        # The assistant goes through what waits on its person, with them (#5110): each go gives the list again.
        person = me["assistant_for"] or _person(conn)
        brief.update(role="assistant", item=None, help_prompt=prompt_for_all(conn, person), assistant_for=person,
                     holds=[item_show(conn, r["id"]) for r in conn.execute(
                         "SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held') ORDER BY id",
                         (actor,))],
                     why=f"you are the assistant of {person}: you go through what waits on them, with them, and take "
                         f"no queue work")
        return brief
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

    tree = None if focus else tree_mode(conn, actor)  # auto_continue tree: only the session's own tree (#4994)

    def try_claim(**kw):
        try:
            got = next_item(conn, claim=True, actor=actor, skipped=skipped, folderless=chat, only=tree, **kw)
        except RiverError as e:
            brief["claim_refused"] = str(e)
            return None
        return got[0] if got else None

    kind, _, fid = (focus or "").partition(":")
    fid, _, person = fid.partition("@")
    # Deploy now on the Targets tab: the session deploys (owning the free target) or reviews the release.
    focus_role_only = role is None and kind in ("deploy", "review")
    if focus_role_only:
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
        got = _deploy_claim(conn, actor, names, take_free=(role == "deployer"), brief=brief,
                            skip_released=focus_role_only)
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
                         why=brief.get("claim_refused") or brief.get("review_refused") or "no release review is ready")
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
    if tree is not None and role in (None, "worker", "unblocker"):
        brief["tree_done"] = True
        brief.update(role="idle", item=None, held_by_others=[],
                     why="no task of your tree (what you worked on in this session) is ready; other work goes to a new "
                         "session with a clean context (auto_continue tree)")
        _set_role_note(conn, actor, "idle", None)
        return brief
    outside = [a for a in open_in_area if a["status"] == "open" and a["blocked_reason"]]
    held_by_others = [a for a in open_in_area if a["status"] in ("in_progress", "held")]
    brief["backlog"] = sum(1 for a in in_area if a["backlog"])
    brief["waits_on_backlog"] = [{"id": a["id"], "title": a["title"], "backlog": a["backlog_blockers"]}
                                 for a in sorted(open_in_area, key=lambda a: a["sort_key"]) if a["backlog_blockers"]]
    needs_plan = not open_in_area or (not held_by_others and len(outside) == len([a for a in open_in_area if a["status"] == "open"]))
    if needs_plan:  # there is no planner role (#4978): the session waits, and says why
        brief["nothing_planned"] = True
        brief.update(role="idle", item=None, held_by_others=[],
                     why=("the project has no open items: the user or the manager adds the next ones"
                          if not open_in_area else "every open item waits on something outside the queue"))
        _set_role_note(conn, actor, "idle", None)
        return brief

    brief.update(role="idle", item=None,
                 held_by_others=[{"id": a["id"], "title": a["title"], "assignee": a["assignee"]} for a in held_by_others],
                 why=("other sessions hold all the work that can move now" if held_by_others
                      else "nothing here can move now, and nothing it waits on is ready"))
    _set_role_note(conn, actor, "idle", None)
    return brief


def released_by(conn, target, actor):
    """True when the last change of the target's owner is this agent's own `maxpm target release`. A deployer
    session's go and wait (MAXPM_FOCUS deploy:) then leave the target free: the agent let it go, and only
    `maxpm target own` or `maxpm go --role deployer` takes it again (#4473: after wait said END, the agent
    released the target, and its next go owned it again for owner_ttl)."""
    head = f"target {target} "
    owner_changes = ("released", "owned by ", "given to ", "taken over ", "ownership expired")
    for r in conn.execute("SELECT actor, change FROM events WHERE item_id IS NULL AND substr(change, 1, ?)=? "
                          "ORDER BY id DESC", (len(head), head)):
        rest = r["change"][len(head):]
        if rest.startswith(owner_changes):
            return r["actor"] == actor and rest == "released"
    return False


def _deploy_claim(conn, actor, names, take_free=False, brief=None, skip_released=False):
    """Claim a ready deploy item on a target the actor owns; with take_free, first own a free target of these
    projects. skip_released (the role comes from MAXPM_FOCUS, not from --role): skip a target the actor released."""
    if take_free:
        targets = [r["target"] for r in conn.execute(
            f"SELECT DISTINCT target FROM projects WHERE target IS NOT NULL AND name IN ({','.join('?' * len(names))})",
            names)] if names else []
        if skip_released:
            targets = [t for t in targets if not released_by(conn, t, actor)]
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
    hooked = hooked_targets(conn) if _is_ai(conn, actor) else set()
    ready = sorted((a for a in ann.values() if a["kind"] == "deploy" and a["target"] in owned and a["ready"]
                    and a["target"] not in hooked), key=lambda a: a["sort_key"])
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
    return {"target": {"name": t["name"], "description": t["description"], "owner": t["owner"], "monitor": t["monitor"]},
            "hooks": target_hooks(conn, t["name"]),
            "ships": item["waits_on_detail"], "release": release_plan(conn, t["name"]),
            "next_deploy": [item_show(conn, r["id"]) for r in nxt]}


def _set_role_note(conn, actor, role, item_id):
    note = f"role: {role}" + (f" on #{item_id}" if item_id else "")
    with tx(conn):
        # A role with work ends a wait; idle keeps the wait clock, so wait_max counts from the first wait.
        keep = role in ("idle", "waiting")
        # An expert stays an expert when a person has it claim an item (#4955), and an assistant an assistant (#5110);
        # maxpm manage still makes a manager.
        conn.execute("UPDATE agents SET note=?, role=?, waiting_since=CASE WHEN ? THEN waiting_since END, "
                     "waiting_in=CASE WHEN ? THEN waiting_in END WHERE name=? AND (COALESCE(role, '') NOT IN "
                     "('expert', 'assistant') OR ?='manager')", (note, role, keep, keep, actor, role))


def focus_role(focus):
    """The role a session's MAXPM_FOCUS gives each of its maxpm go: deployer (deploy:<target>), reviewer
    (review:<target>), else None (the other focuses act on the first go only)."""
    return {"deploy": "deployer", "review": "reviewer"}.get((focus or "").partition(":")[0])


# A ready item wakes one waiting session first (_first_waiter); the other waiting sessions that could take it wake
# for it only when it is still ready this long after their wait first saw it (#5205).
WAIT_SHARE = timedelta(seconds=20)


def _first_waiter(conn, item_id):
    """The waiting session that a newly ready item wakes first: of the agents in maxpm wait for project work
    (waiting_in names the item's project) whose next_item would give them the item (their tree, model and agent
    type), the one that waits longest; None when no waiting session can take it. One ready item in a plan that many
    sessions share (the tree of auto_continue tree follows links both ways) woke each of them, and all but the first
    to run go got IDLE (#5205)."""
    it = conn.execute("SELECT p.name FROM items i JOIN projects p ON p.id=i.project_id WHERE i.id=?",
                      (item_id,)).fetchone()
    if not it:
        return None
    for w in conn.execute("SELECT name, waiting_in FROM agents WHERE kind='ai' AND role='waiting' AND wait_token IS "
                          "NOT NULL AND waiting_since IS NOT NULL AND waiting_in IS NOT NULL "
                          "ORDER BY waiting_since, name"):
        if it["name"] not in w["waiting_in"].split(","):
            continue
        try:
            got = next_item(conn, w["waiting_in"], None, False, w["name"], 50, only=tree_mode(conn, w["name"]))
        except RiverError:
            continue
        if any(a["id"] == item_id for a in got):
            return w["name"]
    return None


def _work_for(conn, actor, names, role=None, seen=None):
    """Why this agent has something to do now, or None: a push to it, ready work it can take, or messages. With
    role (focus_role), only what maxpm go gives that role: a ready deploy item of a target the agent owns or
    can own, or a ready release review; never a push or an item that go refuses to the role.

    seen (from maxpm wait): {item id: (when this wait first saw it ready, the session it wakes first)}. A ready item
    that wakes another waiting session first (_first_waiter) is work here only WAIT_SHARE after this wait first saw
    it, so that session takes it, and the others do not each wake for it and get IDLE from go (#5205)."""
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
                    f"AND p.name IN ({','.join('?' * len(names))})", names) if not released_by(conn, r["name"], actor)}
            got = next((a for a in sorted(ann.values(), key=lambda a: a["sort_key"])
                        if a["kind"] == "deploy" and a["target"] in targets - hooked_targets(conn) and a["ready"]), None)
        else:
            got = next((a for a in sorted(ann.values(), key=lambda a: a["sort_key"]) if a["kind"] == "review"
                        and a["ready"] and a["reserved_for"] in (None, actor) and not reviewer_refusal(conn, a, actor)), None)
        if got:
            return f"#{got['id']} is ready: {got['title']}"
        u = unread(conn, actor)
        return "you have messages (maxpm inbox)" if u["unread"] or u["questions"] else None
    # A push counts only when go can take it (ready_for, as in _go). A pushed item that waits on a conflict or
    # a prerequisite is no work yet: else wait returns at once and go gives IDLE, a loop with no end (#4318).
    pushed = [r["id"] for r in conn.execute("SELECT id FROM items WHERE reserved_for=? AND reserved_until IS NOT NULL "
                                            "AND status='open'", (actor,))]
    if pushed:
        ann = annotate(conn)
        got = next((i for i in pushed if i in ann and ready_for(ann[i], actor)), None)
        if got:
            return f"#{got} was pushed to you"
    if note:
        return f"your queue has {'a stop request' if note['kind'] == 'stop' else 'an instruction'}: {note['body']}"
    q = _queue_ready(conn, actor)
    if q:
        return f"#{q[0]['id']} in your queue is ready: {q[0]['title']}"
    try:
        nxt = next_item(conn, ",".join(names) if names else None, None, False, actor, 1 if seen is None else 5,
                        only=tree_mode(conn, actor))
    except RiverError:
        nxt = []
    for a in nxt:
        if seen is not None:
            if a["id"] not in seen:
                seen[a["id"]] = (now(), _first_waiter(conn, a["id"]))
            first_at, first = seen[a["id"]]
            if first not in (None, actor) and now() < first_at + WAIT_SHARE:
                continue  # it wakes `first`; this wait takes it only if it is still ready after WAIT_SHARE
        return f"#{a['id']} is ready: {a['title']}"
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
    first wait, so river released the agent's goals and unregistered it, and the session should stop; or left: this
    wait has nothing more to do (the session took work, or a newer wait of it runs), and the session does nothing.
    A Claude Code session runs it as a background command, so a typed message reaches the session at once (#4948).
    An agent with no background commands (its adapter's LAUNCH['background'] is false: Pi, Codex) runs it in the
    foreground, and its CLI keeps a typed message until the command ends; so a person's text typed into its pane
    (typed_to) ends the wait at its next poll with result message: the agent reads the text and answers it, then
    waits again (#5256)."""
    import secrets
    import time
    sleep = sleep or time.sleep
    if not actor:
        raise RiverError("waiting needs an agent name: pass --as <name>")
    if _agent(conn, actor)["role"] == "expert":
        raise RiverError(f"{actor} is an expert session: it waits at its prompt for the person, not in maxpm wait, "
                         f"and MaximizePM never ends it for that. Answer the person; maxpm --as {actor} go shows your "
                         f"news")
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
        # The newest wait of an agent is its wait: one that runs in the background (Claude Code) is still there
        # when the agent, after a talk with a person, starts another one (#4948).
        token = secrets.token_hex(4)
        conn.execute("UPDATE agents SET wait_token=?, wait_at=? WHERE name=?", (token, iso(now()), actor))
    since = parse_iso(_agent(conn, actor)["waiting_since"])
    limit = parse_duration(setting(conn, "wait_max", agent=actor))
    step = parse_duration(step or setting(conn, "wait_step", agent=actor))
    deadline = min(now() + step, since + limit)
    seen = {}  # the ready items this wait saw, for _work_for: one waiting session wakes for each first (#5205)
    started = _agent(conn, actor)["wait_at"]
    ad = _adapters().find(session_type(conn, actor) or "")
    foreground = not (ad and ad.launch.get("background"))

    def typed():
        """A person typed into the pane since this wait started, and the agent's CLI holds the text until the
        command ends (a foreground wait): end it, so the agent reads the text now."""
        if not foreground:
            return False
        t = conn.execute("SELECT typed_at FROM agents WHERE name=?", (actor,)).fetchone()
        return bool(t and t["typed_at"] and started and t["typed_at"] >= started)

    def left():
        """Why this wait has nothing more to do, or None: the agent ran go and took work (its wait ended), or a
        newer maxpm wait of the agent runs. The session was talking with a person while this one ran in the
        background, so this one ends quietly."""
        a = conn.execute("SELECT waiting_since, wait_token FROM agents WHERE name=?", (actor,)).fetchone()
        if not a:
            return "it is not registered any more"
        if a["wait_token"] != token:
            return "a newer maxpm wait of this session runs"
        if not a["waiting_since"] or conn.execute("SELECT 1 FROM items WHERE assignee=? AND status IN "
                                                  "('in_progress','held')", (actor,)).fetchone():
            return "this session took work since this wait started"
    while True:
        st = stopped()
        if st:
            return st
        gone = left()
        if gone:
            return {"result": "left", "agent": actor, "why": gone}
        if typed():
            return {"result": "message", "agent": actor, "why": "a person typed to you in your terminal"}
        why = _work_for(conn, actor, names, role, seen)
        if why:
            return {"result": "work", "why": why, "agent": actor}
        if now() >= deadline:
            break
        poll_activity(conn, actor)  # a waiting session is active: it takes work within seconds
        sleep(poll)
    # A person's prompt during the wait moved waiting_since (activity_event): the clock counts from the talk.
    since = parse_iso(_agent(conn, actor)["waiting_since"] or iso(now()))
    if now() < since + limit:
        return {"result": "again", "agent": actor, "left": _short(since + limit - now())}
    with tx(conn):
        _release_goals(conn, actor, f"{actor} ended after waiting")
    try:
        unregister(conn, actor, "maxpm")
    except RiverError as e:  # it owns a deploy target: keep it registered, and say so
        return {"result": "end", "agent": actor, "waited": _short(limit), "kept": str(e)}
    return {"result": "end", "agent": actor, "waited": _short(limit)}


def typed_to(conn, agent):
    """A person typed text into the agent's pane (the Sessions message box, the text of a prompt answer): a
    foreground maxpm wait of the agent ends at its next poll, so the agent reads the text at once (#5256)."""
    with tx(conn):
        conn.execute("UPDATE agents SET typed_at=? WHERE name=?", (iso(now()), agent))


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


def end_idle(conn, agent, why, force=False):
    """End an agent that is idle at its prompt and holds nothing: tell the senders of what it never read (a
    message, an instruction in its queue), release its goals, and unregister it. Returns None when it holds an
    item or owns a target (it stays). force (a stopped agent, end_stopped): it ends also while it waits on a
    person's answer, since nobody needs its work any more; it must hold nothing."""
    with tx(conn):
        if conn.execute("SELECT 1 FROM items WHERE assignee=? AND status IN ('in_progress','held')", (agent,)).fetchone() \
                or conn.execute("SELECT 1 FROM targets WHERE owner=?", (agent,)).fetchone() \
                or (not force and waits_on_person(conn, {agent})):
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


# ---------------------------------------------------------------- experts
# An expert session (#4955) is one that a person talks with for hours or days, like the session that wrote a
# research paper and that the person wants to keep asking. MaximizePM never ends it as it ends a worker (wait END,
# idle_end, hand_over), sends it no queue work, and gives no alert about its prompt; it answers the person. It
# ends when a person ends it (maxpm expert end) or after expert_idle_end with no talk (end_quiet_experts).

EXPERT_NOTE = "expert: "  # the history line that records an expert's topic (its start again reads it)


def _expert(conn, name):
    a = _agent(conn, name)
    if a["role"] != "expert":
        raise RiverError(f"{name} is not an expert session; make one: maxpm expert start <name> --topic \"...\", or "
                         f"in the session itself: maxpm --as <name> expert adopt --topic \"...\"")
    return a


def expert_notes_name(name):
    """The canvas that holds an expert's notes: its topic, what it learned, and the decisions of its talks."""
    return "expert-" + re.sub(r"[^a-z0-9._-]", "-", name.lower())[:57]


def _make_expert(conn, name, topic, project=None, actor=None):
    """Inside a tx: make a registered agent an expert session (role expert, its topic, its project)."""
    t = iso(now())
    was = _agent(conn, name)
    conn.execute("UPDATE agents SET role='expert', note=?, expert_topic=?, expert_project=COALESCE(?, expert_project), "
                 "talked_at=COALESCE(talked_at, ?), waiting_since=NULL, waiting_in=NULL, wait_token=NULL, stop_at=NULL, "
                 "stop_by=NULL, stop_reason=NULL WHERE name=?", (f"{EXPERT_NOTE}{topic[:120]}", topic, project, t, name))
    if was["role"] != "expert" or was["expert_topic"] != topic:
        _event(conn, None, actor or name, f"{name} is an {EXPERT_NOTE}{topic}" + (f" (project {project})" if project else ""))


def assistant_make(conn, name, person):
    """Make a new agent the assistant of a person (#5110, mark 2026-11-10: 'when we start an agent from the You session
    to clean up those items it needs to be a longer lived agent'): role assistant, the person whose list it goes
    through. It takes no queue work and no lease, uses no worker slot (live_sessions, capacity), and MaximizePM never
    ends it by the worker rules (idle_end, wait_max, hand-over): only a person ends it (End, maxpm stop)."""
    register(conn, name, note=f"role: assistant: goes through what waits on {person}, with them")
    with tx(conn):
        conn.execute("UPDATE agents SET role='assistant', assistant_for=? WHERE name=?", (person, name))
        _event(conn, None, name, f"assistant of {person}")
    return agent_status(conn, name)


def assistant_of(conn, person):
    """The assistant of a person that runs now (not stopped, not gone), or None: one per person (#5110)."""
    for r in conn.execute("SELECT * FROM agents WHERE role='assistant' AND kind='ai' AND assistant_for=? "
                          "ORDER BY registered_at DESC", (person,)).fetchall():
        if _agent_state(conn, r) not in ("stopped", "gone"):
            return r["name"]
    return None


def expert_make(conn, name, topic, project=None, actor=None):
    """Make an agent an expert session: role expert, its topic, the project whose folder it works in. A name that is
    not registered is registered (maxpm expert start names the new session). The agent keeps what it holds."""
    topic = " ".join((topic or "").split())
    if not topic:
        raise RiverError("an expert needs a topic: what it knows and what the person asks it about, "
                         "--topic \"...\"")
    if project:
        _project(conn, project)
    r = conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
    if r is None:
        register(conn, name, note=f"{EXPERT_NOTE}{topic[:120]}")
    elif r["kind"] == "human":
        raise RiverError(f"{name} is a person; an expert is an agent session")
    with tx(conn):
        _make_expert(conn, name, topic, project, actor)
        conn.execute("UPDATE agents SET talked_at=? WHERE name=?", (iso(now()), name))
    return expert_view(conn, _agent(conn, name))


def expert_task(conn, name):
    """The expert-tier task of an expert: the one it holds, else the newest open one it claimed before (its start
    again takes it), or None."""
    r = conn.execute("SELECT * FROM items WHERE assignee=? AND tier='expert' AND status IN ('in_progress','held') "
                     "ORDER BY id LIMIT 1", (name,)).fetchone()
    if r:
        return r
    return conn.execute("SELECT i.* FROM events e JOIN items i ON i.id=e.item_id WHERE e.actor=? AND e.change LIKE "
                        "'claimed%' AND i.tier='expert' AND i.status='open' ORDER BY e.id DESC LIMIT 1",
                        (name,)).fetchone()


def expert_handover_check(conn, canvases=(), sessions=()):
    """Before an expert starts: each canvas and each session of its handover exists, so a typo refuses the start and
    adds no task."""
    for ref in canvases or ():
        if ref and ref.strip() and not conn.execute(
                "SELECT 1 FROM canvases WHERE (name=? OR 'c' || id=? OR CAST(id AS TEXT)=?) AND deleted_at IS NULL",
                (ref.strip().lower(), ref.strip(), ref.strip())).fetchone():
            raise RiverError(f"no canvas {ref.strip()}; the canvases: maxpm canvas list")
    for name in sessions or ():
        if name and name.strip() and not conn.execute("SELECT 1 FROM agents WHERE name=?", (name.strip(),)).fetchone():
            raise RiverError(f"no session {name.strip()}; the sessions: maxpm who --all")


def _proposal(conn, pid):
    r = conn.execute("SELECT * FROM expert_proposals WHERE id=?", (int(str(pid).lstrip("pP")),)).fetchone()
    if r is None:
        raise RiverError(f"no expert proposal {pid}; the open ones: maxpm expert proposals")
    out = dict(r)
    for k in ("sources", "canvases", "sessions"):
        out[k] = json.loads(out[k] or "[]")
    return out


def expert_proposals(conn, manager=None, state="open"):
    """The expert proposals (#4990), newest first: the open ones (state None: all), of one manager or of all."""
    q, args = "SELECT id FROM expert_proposals WHERE 1=1", []
    if state:
        q += " AND state=?"; args.append(state)
    if manager:
        q += " AND manager=?"; args.append(manager)
    return [_proposal(conn, r["id"]) for r in conn.execute(q + " ORDER BY id DESC", args).fetchall()]


def expert_propose(conn, name, topic=None, project=None, item=None, model=None, effort=None, summary=None,
                   sources=(), canvases=(), sessions=(), actor=None):
    """maxpm expert propose (#4990, mark: 'long talks the manager says lets continue in a expert. gives you a button
    to click and it takes you to the expert with the context. (you can dismiss if you just want to talk to manager)').
    The manager records what maxpm expert start needs and starts nothing: its chat on the Sessions tab shows a card
    with the topic and the model, Continue in an expert and Dismiss. A topic or a name that the person dismissed
    in this manager's talk is refused. Returns the proposal."""
    if not actor:
        raise RiverError("propose as the session the person talks with: maxpm --as <you> expert propose <name> ...")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", name or ""):
        raise RiverError("an expert's name uses letters, digits, '.', '_', '-' (up to 64)")
    expert_handover_check(conn, canvases, sessions)
    old = conn.execute("SELECT role FROM agents WHERE name=?", (name,)).fetchone()
    if old is not None and old["role"] != "expert":
        raise RiverError(f"{name} is a session that runs now ({old['role'] or 'no role'}); pick another name")
    task = _item(conn, item) if item is not None else None
    topic = (topic or "").strip() or (task["title"] if task is not None else "")
    if not topic:
        raise RiverError("say what the talk is about: --topic \"...\" (or --item <id> of the task it takes)")
    is_manager = conn.execute("SELECT 1 FROM agents WHERE name=? AND role='manager' AND kind='ai'", (actor,)).fetchone()
    if is_manager and task is None and not (summary or "").strip():
        raise RiverError(f"the expert gets the talk so far: add --summary \"<what was said, what is decided, what is "
                         f"open>\" (or --summary-file <path>), and --canvas <name>, --source <file or url>, --session "
                         f"{actor} for what it must read")
    for r in conn.execute("SELECT id, name, topic FROM expert_proposals WHERE manager=? AND state='dismissed'",
                          (actor,)).fetchall():
        if r["name"] == name or (r["topic"] or "").strip().lower() == topic.lower():
            raise RiverError(f"the person dismissed the expert on this topic (proposal p{r['id']}): continue the talk "
                             f"yourself, and do not propose it again in this talk")
    if conn.execute("SELECT 1 FROM expert_proposals WHERE name=? AND state='open'", (name,)).fetchone():
        raise RiverError(f"an expert {name} is proposed already: maxpm expert proposals")
    linked = task or next((_item(conn, int(m)) for m in re.findall(r"#(\d+)", topic)
                           if conn.execute("SELECT 1 FROM items WHERE id=?", (int(m),)).fetchone()), None)
    if model:
        why = "the manager chose it" if is_manager else "chosen"
    else:
        model, why = session_model(conn, linked) if linked is not None else (None, None)
        if not model:
            raise RiverError(f"say which model the expert {name} runs: --model <name> (the card shows it, and the "
                             f"person can change it); or name the item whose work it takes up (#<id>, or --item <id>)")
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO expert_proposals(manager,name,topic,project,item_id,model,model_why,effort,summary,sources,"
            "canvases,sessions,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (actor, name, topic, project, task["id"] if task is not None else None, model, why, effort,
             (summary or "").strip() or None, json.dumps(list(sources or ())), json.dumps(list(canvases or ())),
             json.dumps(list(sessions or ())), iso(now())))
        _event(conn, task["id"] if task is not None else None, actor,
               f"proposed the expert {name} on {topic!r} ({model}): the person starts it or dismisses it")
    return _proposal(conn, cur.lastrowid)


def expert_request(conn, topic, actor=None, model=None, person=None, name=None, context=None, item=None,
                   project=None, cwd=None, blocked=False):
    """maxpm expert request (#5036): a session asks for an expert, often on a person's word ('ask the manager to
    setup a fable expert to review your copy'). A request in free text went wrong: the manager added a plain task
    and launched a worker. So the request itself adds the expert-tier task (found during the requester's item, with
    the model asked for), and alerts the manager, or the person when no manager runs, with the exact line that starts
    the expert. maxpm launch --item <id> of that task starts an expert too. Returns the task and the start line."""
    if not actor:
        raise RiverError("ask as the session that needs the expert: maxpm --as <you> expert request \"<topic>\" ...")
    topic = (topic or "").strip()
    if not topic:
        raise RiverError("say what the expert does: maxpm expert request \"<topic>\"")
    if name is not None and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", name):
        raise RiverError("an expert's name uses letters, digits, '.', '_', '-' (up to 64)")
    if name is not None and conn.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
        raise RiverError(f"{name} is a session already; pick another name, or leave out --name")
    held = _item(conn, item) if item is not None else _held_item(conn, actor)
    if held is not None:
        held = _item(conn, held["id"])
    if person is not None:
        p_row = _agent(conn, person)
        if p_row["kind"] != "human":
            raise RiverError(f"{person} is an agent; --for names the person who talks with the expert")
    if project is None:
        if held is not None:
            project = _project_name(conn, held["project_id"])
        else:
            project = next(iter(projects_for_dir(conn, cwd)), None) if cwd else None
    if not project:
        raise RiverError("name the project the expert works in: --project <name>")
    if not model:
        model = session_model(conn, held)[0] if held is not None else None
    if not model:
        raise RiverError("say which model the expert runs: --model <name> (the person's word, else the model of "
                         "the work it continues)")
    lines = [f"Expert request of {actor}" + (f" for {person}" if person else "")
             + (f", found during #{held['id']}" if held is not None else "") + "."]
    if (context or "").strip():
        lines.append(context.strip())
    if name is None:
        base = f"{project}-expert"
        name, n = base, 1
        while conn.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
            n += 1
            name = f"{base}-{n}"
    iid = item_add(conn, project, topic, actor=actor, context="\n".join(lines), tier="expert",
                   found_during=held["id"] if held is not None else None,
                   models={"model": model, "max_model": model})["id"]
    start = f"maxpm expert start {name} --item {iid}"
    to = active_manager(conn) or person or next(
        (r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY last_seen DESC")), None)
    sent = None
    if to:
        body = (f"EXPERT REQUEST: {topic} ({model})" + (f", for {person}" if person else "") + f". It is the "
                f"expert-tier task #{iid}. Start it as an expert: {start}   (or maxpm launch --item {iid}; never a "
                f"plain task for a worker)")
        sent = send(conn, "alert", body, to=to, item=iid, actor=actor, blocked=blocked)["id"]
    return {"expert_request": {"item": iid, "topic": topic, "model": model, "name": name, "project": project,
                               "start": start, "to": to, "message": sent,
                               "found_during": held["id"] if held is not None else None}}


def expert_proposal_end(conn, pid, state, actor=None, why=None, expert=None):
    """Close an open proposal: started (the person clicked Continue in an expert) or dismissed. The manager hears
    it: after a start, it points the next turn about the topic to the expert; after Dismiss, it talks on itself."""
    if state not in ("started", "dismissed"):
        raise RiverError("a proposal ends started or dismissed")
    with tx(conn):
        p = _proposal(conn, pid)
        if p["state"] != "open":
            raise RiverError(f"expert proposal p{p['id']} is {p['state']} already")
        conn.execute("UPDATE expert_proposals SET state=?, decided_at=?, decided_by=?, why=? WHERE id=?",
                     (state, iso(now()), actor, why, p["id"]))
        who = actor or "the person"
        text = (f"{who} took your proposal p{p['id']}: the expert {expert or p['name']} started on {p['topic']!r} "
                f"({p['model']}). Answer the next turn about it with one line that points to the expert."
                if state == "started" else
                f"{who} dismissed your proposal p{p['id']} of the expert {p['name']} on {p['topic']!r}"
                + (f" ({why})" if why else "") + ": continue the talk yourself, and do not propose it again in this talk.")
        if conn.execute("SELECT 1 FROM agents WHERE name=?", (p["manager"],)).fetchone():
            _send(conn, "note", actor or "maxpm", text, to=p["manager"], item_id=p["item_id"])
    return _proposal(conn, p["id"])


def expert_handover(conn, task_id, actor=None, summary=None, sources=(), canvases=(), sessions=()):
    """The talk so far goes into the expert's task (#4969): when a talk with the manager (or another session) becomes
    a long talk about one topic, the expert that takes it over reads what was said, the sources, the canvases, and
    the sessions to read, from its task, not from the manager's context. It is added at the end of the task's
    context, so a task that had a context keeps it. Each canvas must exist; each session must be a known agent, and
    its newest transcript on this computer is named. Returns the block, or None when nothing was given."""
    summary = (summary or "").strip()
    sources = [s.strip() for s in sources or () if s and s.strip()]
    canvases = [c.strip() for c in canvases or () if c and c.strip()]
    sessions = [s.strip() for s in sessions or () if s and s.strip()]
    if not (summary or sources or canvases or sessions):
        return None
    task = _item(conn, task_id)
    project = _project_name(conn, task["project_id"])
    lines = [f"THE TALK SO FAR (handed over by {actor or 'a person'}, {iso(now())[:10]}):"]
    if summary:
        lines.append(summary)
    if sources:
        lines.append("Sources: " + "; ".join(sources))
    for ref in canvases:
        try:
            c = _canvas_row(conn, ref, project)
        except RiverError:
            c = _canvas_row(conn, ref)
        where = _project_name(conn, c["project_id"])
        lines.append(f"Canvas {c['name']}" + (f" ({c['title']})" if c["title"] else "") + ": maxpm canvas show "
                     f"{c['name']}" + (f" --project {where}" if where != project else "") + " --text")
    from . import sessions as sess
    for name in sessions:
        a = conn.execute("SELECT name, role FROM agents WHERE name=?", (name,)).fetchone()
        if a is None:
            raise RiverError(f"no session {name}; the sessions: maxpm who --all")
        kind, path = sess.transcript(conn, name)
        sid = last_claude_session(conn, name)
        lines.append(f"Session {name}" + (f" ({a['role']})" if a["role"] else "") + ": "
                     + (f"its transcript {path}" if path else f"Claude Code session {sid}" if sid
                        else "no transcript on this computer")
                     + "; read the part about this topic")
    block = "\n".join(lines)
    old = (task["context"] or "").strip()
    item_edit(conn, task["id"], actor=actor, context=(old + "\n\n" + block) if old else block)
    with tx(conn):
        _event(conn, task["id"], actor or "maxpm", "the talk so far handed over to the expert task"
               + (f" ({len(canvases)} canvas(es), {len(sessions)} session(s), {len(sources)} source(s))"
                  if canvases or sessions or sources else ""))
    return block


def agent_type_of(conn, name):
    """The agent type an agent ran in (agents.agent_type), also after it ended: its row, else its last history
    line 'agent type <type>' (set_agent_type); None when neither says."""
    r = conn.execute("SELECT agent_type FROM agents WHERE name=?", (name,)).fetchone()
    if r and r["agent_type"]:
        return r["agent_type"]
    e = conn.execute("SELECT change FROM events WHERE item_id IS NULL AND actor=? AND change LIKE 'agent type %' "
                     "ORDER BY id DESC LIMIT 1", (name,)).fetchone()
    return e["change"][len("agent type "):] if e else None


def expert_topic_of(conn, name):
    """(topic, project) of an expert from the queue, also after it ended: its row, else its last history line."""
    r = conn.execute("SELECT expert_topic, expert_project FROM agents WHERE name=? AND role='expert'", (name,)).fetchone()
    if r and r["expert_topic"]:
        return r["expert_topic"], r["expert_project"]
    e = conn.execute("SELECT change FROM events WHERE item_id IS NULL AND change LIKE ? ORDER BY id DESC LIMIT 1",
                     (f"{name} is an {EXPERT_NOTE}%",)).fetchone()
    if not e:
        return None, None
    m = re.match(rf"^{re.escape(name)} is an {re.escape(EXPERT_NOTE)}(.*?)(?: \(project ([^)]+)\))?$", e["change"])
    return (m.group(1), m.group(2)) if m else (None, None)


def last_claude_session(conn, name):
    """The newest Claude Code session id that the agent ran a maxpm command in (agent_sessions), or None."""
    r = conn.execute("SELECT session_id FROM agent_sessions WHERE agent=? ORDER BY first_seen DESC, rowid DESC LIMIT 1",
                     (name,)).fetchone()
    return r["session_id"] if r else None


def expert_view(conn, a):
    t = a["talked_at"] or a["last_seen"]
    limit = parse_duration(setting(conn, "expert_idle_end", agent=a["name"]))
    task = conn.execute("SELECT id, title FROM items WHERE assignee=? AND tier='expert' AND status IN "
                        "('in_progress','held') ORDER BY id LIMIT 1", (a["name"],)).fetchone()
    return {"name": a["name"], "topic": task["title"] if task else a["expert_topic"], "task": task and task["id"],
            "project": a["expert_project"], "model": a["model"],
            "talked_at": t, "state": _agent_state(conn, a), "session": a["session"], "session_url": a["session_url"],
            "notes": expert_notes_name(a["name"]),
            "ends_at": iso(parse_iso(t) + limit) if limit.total_seconds() else None,
            "holds": [r["id"] for r in conn.execute("SELECT id FROM items WHERE assignee=? AND status IN "
                                                    "('in_progress','held') ORDER BY id", (a["name"],))],
            "unread": unread(conn, a["name"])}


def expert_list(conn):
    return [expert_view(conn, a) for a in conn.execute("SELECT * FROM agents WHERE role='expert' AND kind='ai' "
                                                       "ORDER BY COALESCE(talked_at, last_seen) DESC")]


def _may_end_expert(conn, name, actor):
    """A person, the active manager, or the expert itself ends an expert; another agent may not."""
    if not actor or actor == name:
        return
    r = conn.execute("SELECT kind, role FROM agents WHERE name=?", (actor,)).fetchone()
    if not (r and (r["kind"] == "human" or r["role"] == "manager")):
        raise RiverError(f"refused: an agent may not end the expert {name}; a person, the manager, or the expert "
                         f"itself ends it")


def _expert_let_go(conn, name, why, done_task=False):
    """Before an expert ends: its expert-tier task is done (done_task: the talk is over) or goes back to the queue
    (maxpm expert start <name> --resume takes it again); any other item it was told to claim goes back too."""
    out = {"done": [], "released": []}
    for r in conn.execute("SELECT id, tier FROM items WHERE assignee=? AND status IN ('in_progress','held') ORDER BY id",
                          (name,)).fetchall():
        if done_task and r["tier"] == "expert":
            done(conn, r["id"], f"the talk with the expert {name} ended: {why}", name)
            out["done"].append(r["id"])
        else:
            release(conn, r["id"], f"the expert {name} ended ({why}); start it again with its context: maxpm expert "
                                   f"start {name} --resume", name)
            out["released"].append(r["id"])
    return out


def expert_end(conn, name, actor=None, why=None, done_task=False):
    """End an expert now (a person's word): its task is done (done_task) or goes back to the queue, the senders of
    what it did not read hear it, and it is unregistered."""
    _expert(conn, name)
    _may_end_expert(conn, name, actor)
    why = why or f"ended by {actor or name}"
    let = _expert_let_go(conn, name, why, done_task)
    out = end_idle(conn, name, why)
    if out is None:
        raise RiverError(f"{name} cannot end now: it owns a target or waits on a person's answer")
    return {**out, **let, "resume": f"maxpm expert start {name} --resume"}


def end_quiet_experts(conn):
    """One look of maxpm serve: end each expert that nobody typed or sent a prompt to for expert_idle_end (0s: never).
    end_idle tells the senders of what it did not read. Returns {agent: why}."""
    out, t = {}, now()
    for a in conn.execute("SELECT * FROM agents WHERE role='expert' AND kind='ai'").fetchall():
        limit = parse_duration(setting(conn, "expert_idle_end", agent=a["name"]))
        quiet = t - parse_iso(a["talked_at"] or a["last_seen"])
        if not limit.total_seconds() or quiet < limit:
            continue
        why = f"an expert with no talk for {setting(conn, 'expert_idle_end', agent=a['name'])} (expert_idle_end)"
        _expert_let_go(conn, a["name"], why)
        if end_idle(conn, a["name"], why):
            out[a["name"]] = why
    return out


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


RULE_WINDOW = timedelta(hours=24)  # a broken rule of a closed task shows this long after the close
# mark's decision that the deploy is a hook (#4977, 2026-11-10): before it, the manager ran releases by agreement.
DEPLOY_RULE_SINCE = "2026-11-10T05:11:00Z"
AUTOMATION_SETTINGS = ("auto_review", "fresh_sessions", "auto_continue")  # a global value off its default shows


def broken_rules(conn):
    """The role rules that the queue shows broken now (#4976; the role model mark accepted on 2026-11-10), each
    {"key", "rule", "text", "fix"}: a review held by a session that held a task before it; a deploy of a target
    with a deploy hook that a session holds or closed (a session typed the production command); a deploy that a
    manager closed in the last RULE_WINDOW; an expert on a model other than that of its work; and an automation
    setting whose global value is off its default, with who set it and when (a switch left on, the sweep)."""
    out = []
    since = iso(now() - RULE_WINDOW)
    for r in conn.execute("SELECT id, title, assignee FROM items WHERE kind='review' AND status='in_progress' "
                          "AND assignee IS NOT NULL ORDER BY id").fetchall():
        used = used_session(conn, r["assignee"], r["id"]) if _is_ai(conn, r["assignee"]) else None
        if used is not None:
            out.append({"key": f"review:{r['id']}", "rule": "a review starts in a new session",
                        "text": f"review #{r['id']} is held by {r['assignee']}, which held #{used} before",
                        "fix": f"maxpm release {r['id']} --note \"a review starts in a new session\""})
    # When each target got its deploy hook: a deploy counts against the rule only when the hook was there when the
    # session took it (held) or closed it (done); a release before the hook existed ran by hand, as it had to.
    hooked = {r["name"]: r["set_at"] for r in conn.execute(
        "SELECT t.name, h.set_at FROM hooks h JOIN targets t ON t.id=h.target_id WHERE h.event='deploy'")}
    for r in conn.execute("SELECT id, target, status, assignee, claimed_at, closed_at FROM items WHERE kind='deploy' "
                          "AND (status='in_progress' OR (status='done' AND closed_at > ?)) ORDER BY id", (since,)).fetchall():
        by = r["assignee"] if r["status"] == "in_progress" else _done_by(conn, r["id"])
        at = r["claimed_at"] if r["status"] == "in_progress" else r["closed_at"]
        if r["target"] in hooked and by and _is_ai(conn, by) and at and at >= hooked[r["target"]]:
            out.append({"key": f"typed:{r['id']}", "rule": "no session types a production command",
                        "text": f"deploy #{r['id']} of {r['target']} (it has a deploy hook) is "
                                + ("held" if r["status"] == "in_progress" else "closed") + f" by the session {by}",
                        "fix": "maxpm serve runs the hook; a session releases the deploy item: maxpm release "
                               f"{r['id']}" if r["status"] == "in_progress" else "none now; see that it does not happen again"})
        elif r["status"] == "done" and by and r["closed_at"] >= DEPLOY_RULE_SINCE and conn.execute(
                "SELECT 1 FROM agents WHERE name=? AND role='manager' AND kind='ai'", (by,)).fetchone():
            out.append({"key": f"manager-deploy:{r['id']}", "rule": "the manager does not deploy",
                        "text": f"deploy #{r['id']} of {r['target']} was closed by the manager {by}",
                        "fix": f"a deploy hook for {r['target']} (maxpm target hook {r['target']} deploy \"<release "
                               f"command>\") or a deploy task for a worker"})
    ladder = parse_ladder(setting(conn, "model_ladder"))
    for a in conn.execute("SELECT name, model FROM agents WHERE role='expert' AND kind='ai' ORDER BY name").fetchall():
        task = expert_task(conn, a["name"])
        if task is None or not a["model"]:
            continue
        want, why = session_model(conn, task)
        have = ladder_short(ladder, a["model"]) or a["model"]
        if want and have != (ladder_short(ladder, want) or want):
            out.append({"key": f"expert:{a['name']}", "rule": "an expert runs the model of its work",
                        "text": f"the expert {a['name']} runs {have}; its task #{task['id']} calls for {want} ({why})",
                        "fix": f"ask the user; to change it: maxpm expert end {a['name']}, then maxpm expert start "
                               f"{a['name']} --resume --model {want}"})
    for key in AUTOMATION_SETTINGS:
        row = conn.execute("SELECT value FROM settings WHERE scope='global' AND key=?", (key,)).fetchone()
        if row is None or row["value"] == DEFAULT_SETTINGS[key]:
            continue
        ev = conn.execute("SELECT actor, at FROM events WHERE item_id IS NULL AND change LIKE ? ORDER BY id DESC LIMIT 1",
                          (f"setting global {key}=%",)).fetchone()
        out.append({"key": f"setting:{key}", "rule": "an automation runs as its default says",
                    "text": f"{key} is {row['value']} for the whole queue (default {DEFAULT_SETTINGS[key]})"
                            + (f", set by {ev['actor'] or 'a person'} at {ev['at']}" if ev else ""),
                    "fix": f"ask the user whether it is still wanted; else: maxpm config unset {key}"})
    return out


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
            if x["needs_check"] and x["status"] == "open" and not x["reserved_until"] and not x["project_archived"]
            and not x["backlog"]]
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
        f"SELECT id, from_agent, to_agent, body, item_id, held_for, held_until FROM messages WHERE kind='question' "
        f"AND state='open' AND to_agent IN ({','.join('?' * len(humans))}) ORDER BY id", humans)] if humans else []
    loop = serve_loop(conn)
    unneeded = [{"id": e["id"], "title": e["title"], "doer": e["doer"], "project": e["project"],
                 "closes": [{"id": c["id"], "why": c["why"]} for c in e["closes"]]}
                for e in close_open(conn) if not ann[e["id"]]["project_archived"]]
    # A goal with no owner and no open item: nobody else asks whether its done-when holds (#4970).
    goal_checks = []
    for g in conn.execute("SELECT g.*, p.name project FROM goals g JOIN projects p ON p.id=g.project_id "
                          "WHERE g.status='open' AND p.archived=0 ORDER BY p.rank, g.rank, g.id").fetchall():
        if _goal_owner_live(g) or not _goal_emptied(conn, g):
            continue
        last = conn.execute("SELECT i.id, i.title, i.closed_at FROM item_goals ig JOIN items i ON i.id=ig.item_id "
                            f"WHERE ig.goal_id=? AND i.status IN {CLOSED_STATES} ORDER BY i.closed_at DESC, i.id DESC "
                            "LIMIT 1", (g["id"],)).fetchone()
        backlog = [r["id"] for r in conn.execute(
            "SELECT i.id FROM item_goals ig JOIN items i ON i.id=ig.item_id WHERE ig.goal_id=? AND i.status='open' "
            "AND i.backlog_at IS NOT NULL ORDER BY i.id", (g["id"],))]
        goal_checks.append({"goal": g["name"], "project": g["project"], "shared": bool(g["shared"]),
                            "done_when": g["done_when"], "outcome": g["outcome"], "backlog": backlog,
                            "last": dict(last) if last else None, "question": _done_when_question(g, backlog)})
    return {"stuck": stuck, "lost_leases": lost, "waiting_too_long": waiting, "uncovered": uncovered,
            "unneeded": unneeded, "goal_checks": goal_checks,
            "not_connected": unconnected, "serve_loop": [loop] if loop and loop["late"] else [],
            "targets": targets, "questions": questions, "broken_rules": broken_rules(conn),
            "human_ready": [{"id": x["id"], "title": x["title"], "found_during": x["found_during"],
                             "held_for": x["held_for"] or None, "held_until": x["held_until"]} for x in sorted(
                               (x for x in ann.values() if x["ready"] and x["doer"] == "human" and not x["project_archived"]),
                               key=lambda x: x["sort_key"])]}


def _finding_keys(f):
    return sorted({f"stuck:{x['agent']}" for x in f["stuck"]} | {f"lost:{x['id']}" for x in f["lost_leases"]}
                  | {f"waiting:{x['agent']}" for x in f["waiting_too_long"]}
                  | {f"unconnected:{x['agent']}" for x in f.get("not_connected", [])} | {f"uncovered:{x['project']}" + (f":{x['agent_type']}" if x.get("agent_type") else "") for x in f["uncovered"]}
                  | {f"target:{x['target']}" for x in f["targets"]} | {f"question:{x['id']}" for x in f["questions"]}
                  | {"serve_loop" for x in f.get("serve_loop", [])}
                  | {f"unneeded:{x['id']}" for x in f.get("unneeded", [])}
                  | {f"goal:{x['goal']}" for x in f.get("goal_checks", [])}
                  | {f"rule:{x['key']}" for x in f.get("broken_rules", [])}
                  | {f"human:{x['id']}" for x in f["human_ready"]})


def manager_view(conn):
    """The page's Manager section: the active manager (or none), its last actions, what it asked people."""
    name = active_manager(conn)
    if name is None:
        return None
    a = agent_status(conn, name)
    humans = [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human'")]
    gap = watch_gap(conn, name)
    return {"name": name, "platform": a.get("platform"), "model": a.get("model"), "state": a["state"],
            "no_watch": _short(gap) if gap is not None and gap >= WATCH_LATE else None,  # nothing wakes it (#1898)
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
    pol = policies_for(conn, role="manager")
    return {"agent": actor, "new_name": new_name, "role": "manager", "status": status(conn), "findings": f,
            "policies": pol, "other_policies": len(policy_list(conn)) - len(pol),
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
    session, and they do not wake it; the watch brings the others. Each poll and the return record that the
    watch runs (agents.watch_seen): a manager with no watch hears of nothing (watch_gap, #1898). Returns what
    changed."""
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
        waiting, due = _watch_mail(conn, actor, native, waits)
        mail = bool(due)
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
                with tx(conn):  # watch_seen: the watch ran until now (#1898)
                    conn.execute("UPDATE agents SET manage_seen=?, watch_seen=? WHERE name=?",
                                 (json.dumps(sorted(keys)), iso(now()), actor))
            except RiverLocked:
                pass  # the messages are read already, so return them; the next watch names these findings again
            return {"agent": actor,
                    "result": "stop" if st else "change" if keys - base else "messages" if mail else "tick",
                    "new": sorted(keys - base), "gone": sorted(base - keys), "findings": f, "stop": st,
                    # A blocked message first: its sender stands still.
                    "messages": sorted((m for m in rows if m["unread"]), key=lambda m: (not m["blocked"], m["id"])),
                    "still_open": sum(1 for m in rows if not m["unread"]),
                    "unread": unread(conn, actor)["unread"], "native": native}
        poll_activity(conn, actor, watch=True)
        sleep(poll)


def _watch_mail(conn, actor, native, waits):
    """(waiting, due) for the watch of the manager `actor`. waiting: its unread messages. Unread only: an open
    question already read would wake it at once, every time. Not what the platform brought into the session
    already (native: native_message). due: those the watch returns for now. A blocked message and a person's
    message do not wait (MaximizePM may mark one blocked after the send); another one waits the time of its
    level (waits: {"high": ..., "low": ...})."""
    waiting = [m for m in conn.execute(
        f"SELECT m.level, m.blocked, m.created_at, m.native_status, a.kind sender FROM messages m "
        f"LEFT JOIN agents a ON a.name=m.from_agent WHERE {_TO_ME} "
        f"AND m.from_agent<>? AND m.read_at IS NULL", (actor, actor, actor)).fetchall()
        if not (native and (m["native_status"] is None or m["native_status"] == "sent"))]
    return waiting, [m for m in waiting if m["blocked"] or m["sender"] == "human" or m["level"] == WAKE_LEVEL
                     or parse_iso(m["created_at"]) + waits[m["level"] or DEFAULT_LEVEL] <= now()]


def watch_gap(conn, name):
    """For how long the manager `name` runs no watch (maxpm manage --watch): a timedelta, or None while a
    watch runs (its last poll is younger than WATCH_BEAT), for a manager that never ran one (a manager in a
    chat has none), and for an agent that is no manager."""
    r = conn.execute("SELECT watch_seen FROM agents WHERE name=? AND role='manager'", (name,)).fetchone()
    if not r or not r["watch_seen"]:
        return None
    gap = now() - parse_iso(r["watch_seen"])
    return gap if gap > WATCH_BEAT else None


def tell_watch_late(conn):
    """One pass of maxpm serve (#1898). A manager that runs no watch for WATCH_LATE, while a message for it is
    due (a watch would return for it now), does not hear of the message: nothing wakes a session at its prompt.
    Each person gets one alert in the manager's name (WATCH_NOTE), which opens a needs-you event; the page and
    the notification link open the manager's Terminal for it. When the watch runs again, nothing is due any
    more, or the manager is stopped or gone, MaximizePM marks the alert read, which closes the event. Returns
    {"late": {manager: messages due}, "told": [managers], "closed": [message ids]}."""
    late = {}
    for a in conn.execute("SELECT * FROM agents WHERE role='manager' AND kind='ai' AND watch_seen IS NOT NULL").fetchall():
        gap = watch_gap(conn, a["name"])
        if gap is None or gap < WATCH_LATE or _agent_state(conn, a) not in ("active", "away"):
            continue
        waits = {"high": parse_duration(setting(conn, "manage_wait_high", agent=a["name"])),
                 "low": parse_duration(setting(conn, "manage_wait_low", agent=a["name"]))}
        due = _watch_mail(conn, a["name"], has_native(conn, a["name"]), waits)[1]
        if due:
            late[a["name"]] = (gap, due)
    alerts = conn.execute("SELECT id, from_agent FROM messages WHERE kind='alert' AND read_at IS NULL AND body LIKE ?",
                          (WATCH_NOTE + "%",)).fetchall()
    closed = [m["id"] for m in alerts if m["from_agent"] not in late]
    told = [name for name in late if not any(m["from_agent"] == name for m in alerts)]
    if not told and not closed:
        return {"late": {n: len(d) for n, (_, d) in late.items()}, "told": [], "closed": []}  # the usual pass: no write
    with tx(conn):
        _mark_read(conn, closed)
        people = [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY name")]
        for name in told:
            gap, due = late[name]
            blocked = sum(1 for m in due if m["blocked"])
            body = (f"{WATCH_NOTE} (maxpm manage --watch) for {_short(gap)}: {len(due)} message{'s' if len(due) != 1 else ''} "
                    f"wait{'' if len(due) != 1 else 's'} for it"
                    + (f", {blocked} blocked (the sender stands still)" if blocked else "")
                    + f". Nothing wakes a session at its prompt. Tell it in its terminal to start the watch again: "
                      f"maxpm --as {name} manage --watch")
            for person in people:
                _send(conn, "alert", name, body, to=person)
        told = told if people else []  # with no person in the queue yet, the first one who registers gets it
        if told or closed:
            sync_needs_you(conn)
    return {"late": {n: len(d) for n, (_, d) in late.items()}, "told": told, "closed": closed}
