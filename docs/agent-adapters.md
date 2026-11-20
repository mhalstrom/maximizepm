# Agent adapters: one interface for every coding agent

Decided by mark, 2026-11-11 ("1-3 are good"):

1. tmux stays the one terminal layer. Every agent runs its real CLI in a tmux window. There is no second backend and no fallback path (mark: "I don't want to have a fallback"). ACP, codex app-server and the Claude Agent SDK were rejected for now: Claude Code reaches them only through adapters that do not accept a claude.ai plan login, and the person loses the terminal. herdr was looked at: its ideas are copied (agent detection as data, one state report per agent), not the program.
2. Every agent is one adapter. The adapters replace all platform tables and all name checks.
3. maxpm runs on Windows only inside WSL 2 (#5180).

Multi-provider models (OpenCode, Pi, Goose and others): option A. The model of such an agent is a free "provider/model" text. An item's min-model and max-model limits apply only to the families maxpm knows (claude, openai).

## Today (map of 2026-11-11, paths in river/)

- Four platform tables with no shared key: `core.LAUNCH_PLATFORMS` (core.py:347), `core.AGENT_FAMILIES` and `PLATFORM_EFFORTS` (core.py:11366), `sessions.PLATFORMS` and `sessions.PROVIDERS` (sessions.py:28, :39), plus the `{"claude": "claude-code", "codex": "codex"}` map copied at core.py:1505, :1526, :10750, :11233.
- About 30 functions test a platform name: `build_command` (core.py:10674), `_launch_agent_cmd` (:11217), `profile_from_command` (:10739), `resume_command` (:11294), `agent_type_from_env` (:1809), `wait_background` (:11573), `start_expert` resume (server.py:352, :419), `move_into_tmux` (server.py:1164), the parsers `_claude`/`_codex` (sessions.py:297, :357), `transcript_chain`, `custom_title`, `_transcript_settings` (sessions.py:1237), `_cache_line`, `_line_hits`, usage measurement (core.py:8580-8760), the hook parser `activity_event` (core.py:8446), `background_work` (:8624), pane wording in `prompt_pattern` (core.py:183) and `_prompt_parts`/`_prompt_found` (server.py:1378-1470), the MCP config writers (cli.py:1382-1504), the instruction file names (cli.py:1944-2046), and the JS name checks in views/sessions.js:42-44, :82, :123.
- Effort levels exist in four copies (the `effort_levels` default, `PLATFORM_EFFORTS`, sessions.py:45, Codex's models cache), and the flag forms, the resume regex (core.py:11336) and the live `/effort` command are Claude- or Codex-only.
- `default_agent`, `agent_rules` and an item's `--agent` accept only `claude-code` or `codex` (`_agent_platform`, core.py:1503). No role has its own agent setting. `fresh_expert` takes the first entry; expert and crash resume work only for Claude Code and Codex.
- Grok, OpenCode and Pi exist only as plain custom commands in `server.KNOWN_AGENTS` (server.py:2822): no agent type, no settings, no chat on the page, no resume.

## The adapter

Interface agreed by mark, 2026-11-11 ("the interface is fine"):

The contract as code: `river/agents/__init__.py` (the names and shapes of each part, `check`, the registry and the
loader of the user's modules). The built-in adapters are `river/agents/claude.py` and `river/agents/codex.py`.

- One Python module per agent, `river/agents/<id>.py`, registered in `river/agents/__init__.py`. Data is plain Python dicts (raw strings for regexes), with functions only where data cannot say it. No TOML or JSON file: no parser, no new package, and the tests import the dicts. A user module in `~/.maximizepm/agents/<id>.py` overrides the built-in one or adds a new agent, with no maxpm release.
- Three states: `working` (the agent runs a turn), `idle` (it waits at its prompt), `blocked` (it waits for a person: a permission menu, a question, the trust question). Gone comes from `maxpm release` or the process exit.
- One report command, from the agent side to maxpm, modelled on herdr's `pane report-agent`:

  ```
  maxpm report --state idle|working|blocked [--message "<why blocked>"]
               [--session-id <id>] [--seq <n>] [-- <resume command>]
  maxpm release
  ```

  `--seq` increases with each report, so a late report never replaces a newer one. A hook that knows the session id and the resume command reports them, so maxpm does not find them per agent.
- One declared source for each signal (state, session id), fixed in the adapter, with no chain of fallbacks: either a hook or plugin that the adapter installs and that calls `maxpm report`, or screen rules in the adapter's `DETECT` dict that maxpm's pane watcher applies and that end in the same `maxpm report`.
- `DETECT` holds the process name, the idle prompt pattern, the working pattern (for example "esc to interrupt"), the patterns of each blocked screen, the text of the "tell it instead" option, and the image paste rule.
- herdr's integrations (Apache-2.0) are a starting point for each agent's hook or plugin, changed to call `maxpm report`. maxpm never runs herdr's scripts behind a fake `HERDR_*` environment.

What each agent gives for state (herdr's integration docs, 2026-11-11): Claude Code, hooks for many events (maxpm uses them now); OpenCode, a JS plugin with full state and session id; Pi, a TS extension with full state; Codex, Copilot CLI, Cursor CLI, Grok CLI, hooks that give the session id, state from the screen; Hermes, a plugin that gives the session id, state from the screen; Amp, screen only; Goose and Aider, not checked. Antigravity CLI (#6248): hooks only from files in its home or the project, so state from the screen and the session id from its environment; with the setting `agy_activity_hooks` on (#6351), MaximizePM's hooks in the project's `.agents/hooks.json` add the shell command guard, each turn's start and end, and messages at a turn's end.

Each part below is optional unless marked required; the page shows only what an adapter supports.

1. Describe (required): id, label, exe, aliases, model family (or "multi" for provider/model text), model ids and default, an optional model list reader (Codex: `~/.codex/models_cache.json`).
2. Settings: a list of setting specs. Each spec: key, kind (toggle, choice, text), choices, default, how it goes on the command line, and how it changes in a running session (a live command such as `/model {value}`, or a restart). The Setup section, the launch dialog and the Settings dialog draw from these specs. Config keys stay `<prefix><key>`.
3. Effort: the agent's levels (static, or read from the CLI per model), a map from maxpm's levels (low, medium, high, xhigh, max) to the agent's names, the flag form, a live command or none, and how to read the effort back from the chat log. An agent with no effort gives none, and the page hides effort for it.
4. Launch (required): `build_command(name, folder, model, effort, settings, prompt)` returns the argv. Capabilities: session name, system prompt file, hooks file, extra dirs, fork. `parse_command(argv)` reads an old command back into a profile.
5. Session identity: how maxpm learns the session's id after the start (an env var, a new log file in a known folder, or text in the pane), its web link if any, and the env vars that tell maxpm a command runs inside this agent (replaces `agent_type_from_env`).
6. Chat log: where the log of a session is (by session id or address, compressed or not), `parse(lines)` to the shared entries (assistant, user with images, tool, result, divider, summary, turn state), and optionally: the chain of files across resumes, the title, the cwd, usage and cache, the settings read back, the tool names that write files (for Artifacts).
7. State: one declared source per signal, as above: a hook installer whose hooks call `maxpm report`, or the `DETECT` dict (replaces the wording in `prompt_pattern` and the Claude layout code in `_prompt_parts`). Claude Code's hooks move from `maxpm activity` to `maxpm report` in step 1, with no change of behaviour. Also: whether the agent waits for background commands in the foreground.
8. Input: tmux paste is the same for every agent. The adapter gives the image paste convention (Claude: "[Image #N]"), the text of the "tell it instead" option of a permission menu, and an optional native message command (Codex: `codex queue --thread`; Claude Code, Pi, OpenCode and Copilot CLI: the built-in `wake`). `INPUT["interrupt"]` (#5547): the tmux keys that end a running turn and keep the session, for Stop and Send now on the Sessions tab and `maxpm interrupt`: Esc for Claude Code, Codex, Antigravity CLI, Pi and Amp; Esc twice for OpenCode ("esc again to interrupt"); Ctrl+C for Copilot CLI, Cursor CLI, Grok CLI, Hermes, Goose and Aider. None: the page says that the agent has no such key. `INPUT["interrupt_again"]` (#5563): a slash command that a second Stop of the same turn types, and what it does in a few words, or None. Codex: `{"text": "/stop", "does": "end its background commands"}`, because its Esc leaves the turn's shell commands running. The first Stop says "Stop again to ...", and the page keeps Stop while the session waits at its prompt.
9. Resume: `resume_command(session_ref, model, effort, settings)` or none.
10. Commands (slash command completion, mark 2026-11-11): the agent's built-in slash commands, and a reader for its custom ones for a session folder (Claude: ~/.claude/commands, .claude/commands, the skills in ~/.claude/skills and .claude/skills, plugin commands; Codex: ~/.codex/prompts). Each entry: name, description, argument hint. The page: when the message box starts with "/", a list of matching commands opens above it; Tab completes; Up and Down move in the list while it is open (otherwise they recall messages as now); Esc closes it; the text is sent as now.
11. Setup: the instruction file name (CLAUDE.md, AGENTS.md, ...), the skills folder, the MCP config writer, the voice text, the open-link text.

## Agent choice for each role

A new setting `agent_for.<role>` for the roles worker, manager, reviewer, deployer and expert, at the global, project and item scopes, falling back to `default_agent`. `default_agent`, `agent_rules`, an item's `--agent`, and every start path (launch, Dispatch, serve's fresh, operational, review and deployer starts, manager, expert and fresh_expert, deploy_now) take any registered adapter. Expert resume and crash resume (adopt_sessions, revive_sessions, session_restart, move_into_tmux) go through the adapter's `resume_command`.

## Work, in order

1. Claude Code and Codex become adapters with no change of behaviour; every name check above becomes an adapter call; all four tables and the copied maps go. The whole test suite stays green. This design file goes into the repo as docs/agent-adapters.md in this step.
2. Agent choice is generic: `agent_for.<role>`, `default_agent`, `agent_rules`, `--agent`, expert and crash resume through the adapter.
3. Settings, effort and slash commands come from the adapter: the Setup section, the launch dialog, the Settings dialog of a running session, and the "/" list with Tab in the message box (Claude Code and Codex lists first).
4. One adapter per new agent, in parallel after step 3: Pi, OpenCode, Grok CLI, Hermes, Cursor CLI, GitHub Copilot CLI, Aider, Goose, Amp. Each worker installs the CLI, reads its real flags, log files and hook or plugin system on this Mac, and writes the adapter: describe, settings, effort, launch, identity, state first; then the chat log; then resume and commands. herdr's detection files (Apache-2.0, github.com/herdrdev/herdr) may serve as a start for prompt patterns.
5. Each agent's item ends with its test: it starts from the page with its settings; its chat shows on the Sessions page; the message box reaches it; the state moves between working and idle (and waiting, when it asks a permission); its slash commands complete; resume works if the adapter has it.

## Parallel plan

The queue items of this work, by the order in which they can start. An item starts when every item it waits on is done;
the items of one row run in parallel.

1. #5167 A: the adapter contract, the registry and the user override folder (`river/agents/__init__.py`), Claude Code
   and Codex with their describe part only, the commands `maxpm report` and `maxpm release` (no item), and this file.
   No call site moves. Waits on nothing.
2. After #5167, in parallel:
   - #5181 B1: launch, resume, profile and identity go through the adapter (the start paths of core.py and server.py).
   - #5182 B2: the chat log readers and usage measurement go through the adapter (sessions.py, the usage of core.py).
   - #5183 B3: state through `maxpm report` and `DETECT` (the hooks, `activity_event`, `prompt_pattern`,
     `_prompt_parts`). Claude Code's hooks move from `maxpm activity` to `maxpm report`, with no change of behaviour.
   - #5184 B4: setup and the page go through the adapter (the MCP config writers, the instruction files, the name
     checks of sessions.js).
   - #5186 C3: Tab completion of / commands in the Sessions message box, from the adapter's command list.
   - One adapter module for each new agent, with tests from recorded fixtures: #5170 Pi, #5171 OpenCode, #5172 Grok
     CLI, #5173 Hermes, #5175 Cursor CLI, #5176 GitHub Copilot CLI, #5177 Aider, #5178 Goose,
     #5179 Amp. Each one adds only `river/agents/<id>.py`, its fixtures and its tests; it changes no shared file, so
     they do not conflict with the B items. Each worker installs the CLI and reads its real flags, log files and hook
     or plugin system on this Mac.
3. After #5181: #5168 C1, any registered agent for each role (`agent_for.<role>`, `default_agent`, `agent_rules`,
   `--agent`, expert and crash resume), and #5169 C2, settings and effort from the adapter's setting specs (the Setup
   section, the launch dialog, the Settings dialog).
4. After #5181, #5182, #5183 and #5184: #5185 B5 removes the old platform tables and the copied maps; the whole suite
   stays green.
5. The test of each new agent in maxpm, after #5168, #5169, #5185, #5186 and its adapter item: it starts from the page
   with its settings, its chat shows on the Sessions page, the message box reaches it, its state moves between working
   and idle (and blocked, when it asks a permission), its / commands complete, and resume works if the adapter has it.
   #5187 Pi (after #5170), #5188 OpenCode (#5171), #5189 Grok CLI (#5172), #5190 Hermes (#5173), #5192 Cursor CLI
   (#5175), #5193 GitHub Copilot CLI (#5176), #5194 Aider (#5177), #5195 Goose (#5178),
   #5196 Amp (#5179).

Separate: #5180 makes WSL 2 the one way to run maxpm on Windows (decision 3 above).

## As built in #5167

- An adapter is a module; its parts are module-level names: `DESCRIBE` (+ `read_models`), `SETTINGS`, `EFFORT`
  (+ `read_effort_levels`, `read_effort`), `build_command` with `LAUNCH` (+ `parse_command`), `IDENTITY`,
  `chat_path` with `parse_chat` and `CHAT` (+ `chat_chain`, `chat_title`, `chat_cwd`, `chat_usage`, `chat_settings`),
  `STATE` with `DETECT` (+ `screen_settings`) or `install_hooks`, `INPUT`, `resume_command`, `COMMANDS` (+ `read_commands`), `SETUP`
  (+ `write_mcp_config`). The id is the file name (`claude`, `codex`); the old platform names are aliases
  (`claude-code`).
- `agents.load()` reads the built-in modules, then each `<id>.py` in the `agents` folder beside the queue
  (`~/.maximizepm/agents` by default). A user module replaces the built-in module of its id or adds an agent. A user
  module that does not import or does not pass `agents.check()` is left out, and `agents.errors()` names why; the
  built-in module of that id stays. `agents.get(name)` finds an agent by id or alias.
- The loader requires `DESCRIBE`. A module without `build_command` loads, but `Adapter.missing()` names the launch part
  and `Adapter.build_command` refuses, so no start path can start it.
- `maxpm report` stores the state where the Sessions tab reads the hooks' state now: `agents.activity_at`,
  `activity_event` (`report:working`, `report:idle`, `report:blocked`), and for blocked `activity_prompt` (the
  message), so the row shows a prompt. `--seq` is kept in `agents.report_seq` for each `--session-id`
  (`report_session`): a report whose seq is not above the last one of the same session changes nothing. The session id
  goes into `agent_sessions`, the resume command into `agents.resume_argv` (JSON).
- `maxpm release` with no item sets `activity_event` to `report:gone`: the agent is gone at once, as when its process
  ends. Its next maxpm command makes it present again. `maxpm release <id>` gives an item back, as before.

## As built in #5181

- `claude.py` and `codex.py` have their effort, launch, identity and resume parts. `core.build_command`,
  `_launch_agent_cmd`, `profile_from_command`, `resume_command` (and its effort read-back), `agent_type_from_env`,
  the `wait_background` of the go briefing, the effort check of a start, and the web link of an agent
  with no session link call the adapter. `core.shell_command(argv)` quotes each word; an `agents.Raw` word (the args
  setting) goes in as it is.
- What MaximizePM adds to a start goes to `build_command` as the keys of `agents.LAUNCH_KEYS` (the skill in the
  system prompt, the hooks file, a fork, a resume, the manager's autocompact, the words a person took out), each only
  when `LAUNCH` names the capability. The adapter puts them on its command line in its own way.
- `PLATFORM_EFFORTS` and `AGENT_FAMILIES` are read from the built-in adapters until #5185 removes them. The platform
  names `claude-code` and `codex` stay the keys of `LAUNCH_PLATFORMS` and of the stored settings; each one is an id or
  alias of an adapter.
- Left for the next items: the transcript readers of `_ran_model` and the session id of `move_into_tmux` (#5182, by
  the chat part), the Claude-only expert resume of `start_expert` (#5168), the settings of `LAUNCH_PLATFORMS` and
  `sessions.PROVIDERS` (#5169).

## As built in #5183 (state through maxpm report and DETECT)

- Claude Code's hooks are the claude adapter's: `HOOK_EVENTS`, `hook_settings(report)` and `install_hooks` (the file
  `claude-hooks.json` beside the queue, passed with `--settings`). Each hook runs `maxpm report --hook claude`, which
  reads the hook's JSON on stdin with the fast path that `maxpm activity` had; `maxpm activity` stays as its old name
  for the hook files of sessions that started before.
- A new optional function of the state part, `read_hook(payload)`, turns an agent's own hook input into a hook report
  (`HOOK_REPORT_KEYS`: state, event, session id, message, the call that waits, whether the question shows, the
  background work at a turn end, the typed prompt). `core.hook_report` records it as `maxpm report` records a state
  (`activity_event` is `report:<state>` now, not the hook's event name) and keeps every rule of `activity_event`:
  the manager's alerts, the answered questions, the reminder at a turn end, the wait clock.
- `DETECT` has four more optional layout keys (`menu`, `frame`, `rule`, `hint`); `DETECT_LAYOUT` holds their values
  for an adapter that leaves them out. `server._pane_rule` reads each pane with its agent's adapter: with the
  default `prompt_pattern`, the adapter's `blocked` words; another value of the setting is read for every pane, and
  an empty one turns the watch off, as before. The "tell it instead" option of a prompt answer is the adapter's
  `tell_instead`.
- The pane watcher (`server.watch_prompts`) reports the state of an agent whose `STATE['state']` is `detect` (Codex):
  blocked (a prompt), working (`DETECT['working']`), or idle (`DETECT['idle']`), through `core.report`, only when it
  changed. The silence alert of `activity_watch` stays for agents with hooks, and `background_work` returns nothing
  for an adapter whose `STATE['background']` is false.
- Copies left for #5185: the default of `prompt_pattern` (the built-in adapters' blocked words) and of
  `native_message` (the `INPUT['native']` of Codex, Pi, OpenCode, Copilot CLI, Hermes and Claude Code); a test checks
  that they match the adapters. The built-in command `wake {address} {text}` (#5686, #5691, #5702, #5703) writes the message
  beside the queue for the session's listener: Claude Code's is the hook `maxpm report --wake claude` (asyncRewake),
  Pi's is its state extension (`pi.sendUserMessage`), OpenCode's is its state plugin (`client.session.prompt`,
  delivery steer), Copilot CLI's is an extension of its hook plugin (`session.send`), which Copilot loads only in its
  experimental mode (MaximizePM never passes `--experimental`: it saves that mode in the person's settings). For an
  adapter whose module has `deliver_command(address, text)` (Hermes, #5704), `maxpm serve` is the listener
  (`server.wake_pass`, a thread of its loop): it runs that command when the session's pane shows it idle, never during
  a turn. Hermes: `hermes chat --resume <id> --provider <p> -m <model> -q <text>`; the turn runs in the same session
  of Hermes's gateway, but the classic CLI in the pane does not show it.

## As built in #5182

- The chat log part reads every chat log of the Sessions tab, the Artifacts tab, the prompt cache and the usage
  measurement. `river/sessions.py` keeps only the readers that are the same for every agent: the byte windows, the
  cursors, the caches, the shared entry helpers (`_clip`, `_tool_line`, `plain_tool`, `_user_turn`) and the paging.
  Its functions keep their names (`transcript`, `transcript_chain`, `custom_title`, `_transcript_settings`,
  `cache_state`, `artifacts`); each one asks the adapter of the log's kind (the adapter id: `claude`, `codex`).
- New in the contract: `CHAT["ref"]` (`session`: the log is found by the agent's newest session id; `address`: by
  its native_address when the agent runs this CLI) and `CHAT["cache_write"]` (a call after the cache ran out pays the
  cache write price, or the input price). New optional readers: `parse_chat_line(d)` (one decoded line: the Sessions
  tab parses line by line to keep the byte of each entry; without it, it calls `parse_chat([line])`),
  `chat_cache_line(raw, st)`, `chat_texts(raw)` (the texts where the Artifacts tab looks for files and links;
  `CHAT["file_tools"]` names the tools whose path is a file the session wrote), and `chat_requests(session_ref)`
  (each model call once with its time and token counts, and whether the session started as a fork:
  `core.measure_usage`). `chat_path` takes `root=` (a log folder that a test gives), and
  `chat_chain(path, refs=None)` takes the agent's session ids, by which Claude Code finds its resumed files.
- Claude Code: the parser, the chain, the title, the settings, the cache, the Artifacts texts and the requests
  (`read_transcript`, `FORK_SLACK`) are in `river/agents/claude.py`. Codex: the parser, the settings, the cache, the
  Artifacts texts and its log folder (`$CODEX_HOME/sessions`) are in `river/agents/codex.py`.
- `core._ran_model` finds and reads a log through the platform's adapter, and `server.move_into_tmux` takes the
  native_address of an agent whose `CHAT["ref"]` is `address`. `sessions.PLATFORMS` and `PROVIDERS` stay for #5185
  and #5169.
- Checked: the old and the new readers give the same entries, turns, settings, titles, cache states, Artifacts hits
  and requests on 12 real logs (eight of Claude Code, four of Codex).

## As built in #5168

- `agent_for.<role>` is a setting for each role of `core.AGENT_ROLES` (worker, manager, reviewer, deployer, expert).
  In each scope, from the most specific (item, kind, project, global), `agent_for.<role>` comes before
  `default_agent`: a project's `default_agent` wins over a global `agent_for.<role>`. An item's own `--agent` and
  `agent_rules` come first. The role of an item is `core.item_role`: reviewer for a review, deployer for a deploy
  item, expert for an expert-tier task, else worker.
- An agent type is any registered adapter with a launch part (`core.agent_types()`): `default_agent`,
  `agent_for.<role>`, `agent_rules`, an item's `--agent`, `MAXPM_AGENT_TYPE` and a `@<id>` profile in
  `launch_agents` take it. `core.platform_spec(type)` gives its label, exe, family, prefix and options (Claude Code
  and Codex: `LAUNCH_PLATFORMS` until #5185; another adapter: its DESCRIBE and SETTINGS, plus a first prompt).
- `core.launch_entries` is `launch_agents` plus one implicit profile (`<label>=@<id>`) for each agent type that no
  entry runs, so an agent starts with no `launch_agents` entry. The first entry stays the default start.
- `core.role_agent(conn, role, project_id, item_id)` gives the launch entry of a role. The manager, an expert and
  the focused starts (deploy, review, help, unblock) use it; launch, Dispatch, serve's fresh
  start and the deployer read the item's effective agent, which includes its role.
- Expert resume uses the adapter of the agent type the expert ran in (`core.agent_type_of`: its row, else its
  history), through `resume_command`; an adapter with no resume part starts a fresh expert that reads its notes.
  Crash resume (`move_into_tmux`) finds the adapter of the chat's kind in the registry. A non-built-in adapter's
  settings are config keys since #5169 (<prefix><key>; an adapter with no prefix: the spec's default).

## As built in #5169

- `SETTINGS` holds each agent's launch options: Claude Code's remote_control, permission_mode, model_ids, args,
  skill_prompt, prompt and activity_hooks, and Codex's sandbox, approval, model_ids, args and prompt moved there from
  `core.LAUNCH_PLATFORMS`, which is now built from the built-in adapters (#5185 removes it). A spec has three more
  keys: `config` (False: no config key, only a flag that a restart adds or takes out, as Codex's approve_for_me and
  no_daemon), `session` (the Settings dialog of a running session shows it) and `note`. `DESCRIBE` has `model_live`
  and `model_read`, and `EFFORT` has `label`. `check()` refuses a spec that cannot work (a toggle default other than
  on or off, a choice default outside its choices, a config False setting that is no toggle with a flag, a key
  `model` or `effort`, a key twice).
- `Adapter.options()` gives the config settings, `Adapter.effort_choices()` the levels the dialogs offer (each MaximizePM level
  whose name for the agent, through `EFFORT['map']`, is one of `EFFORT['levels']`), and `Adapter.session_specs()` the Settings dialog: the model, the
  effort, then each session setting, each with how it is read from the command line, whether it changes in place,
  and how a restart applies it. `sessions.PROVIDERS` keeps only the label and the voice text.
- `core.launch_platforms()` lists every registered agent that can start, so `@<id> key=value` in launch_agents, the
  launch dialog (models, efforts, options), Settings > Setup, `maxpm config set <prefix><key>` and the config list
  take an agent that is not built in. An agent with no effort has no Effort field in the launch dialog; a "multi"
  agent offers its own model list (`read_models`).

  settings have no config keys yet (#5169): its profile options are the entry's own, else the spec's default.

## As built in #5186 (commands)

- `COMMANDS` of `claude.py` and `codex.py`: the built-in slash commands that a person types most, with the
  descriptions of Claude Code 2.1.322 and Codex 0.159.1 (read from their programs). `read_commands(folder)` adds the
  custom ones. Claude Code: `.claude/commands` and the skills of `.claude/skills` in the folder and its parents up to
  the repository root (the nearer one wins), then `~/.claude/commands` and `~/.claude/skills` (or
  `$CLAUDE_CONFIG_DIR`), then the commands and skills of each installed plugin that `enabledPlugins` turns on, as
  `<plugin>:<name>`. A skill with `user-invocable: false` is left out. Codex: `~/.codex/prompts/*.md` (or
  `$CODEX_HOME`) as `prompts:<name>`. `river/agents/slash.py` reads the Markdown files and their front matter
  (`description`, `argument-hint`); a user's adapter may use it too.
- `GET /api/session-commands?agent=<name>`: `{agent, provider, folder, commands}`. The folder is the cwd of the
  session's transcript; the agent is its agent type, else the kind of its transcript.
- The page: `components/slashList.js`. The list opens while the caret is in the command's name (the text starts with
  "/" and has no space before the caret), and matches the names that start with the typed text, then those that hold
  it. Enter sends the text as typed; after Up or Down chose a row, Enter completes that row first and then sends.
  A click on a row completes it.

## As built in #5220 (slash commands from the page)

- Each entry of `COMMANDS` has `run`: how the command works from the message box, typed with no arguments. `now`: it
  runs at once, and the chat shows what it does or it changes the session. `menu`: it opens a numbered menu, which
  the page's ! dialog shows and answers. `terminal`: its view or its result shows only in the terminal (a
  full-screen view, a slider, a card printed in the pane). No `run`: the page does not say. The "/" list marks
  `menu` and `terminal`. Claude Code and Codex fill it for every built-in command; the other adapters may.
- `session_send` types the text, waits `ENTER_PAUSE` (0.3 s), then sends Enter. Codex reads keys that come fast as a
  paste: an Enter inside it was a new line in its input box, not a send, and the text could come back into the box
  after the command ran (mark's /model, 2026-11-11).
- A slash command writes no turn to the chat log of Codex, and Claude Code writes some only. So `session_send` looks
  at the pane `COMMAND_LOOK` (0.8 s) after the Enter and returns `command: {name, run, taken}`. taken: `menu` (a
  prompt shows), `waiting` (the command is still in the agent's input box, in the last `BOX_LINES` lines), or `ran`.
  The page's bubble says it, and for a `terminal` command it links to the terminal.
- Claude Code's `local_command` lines show in the chat: the command as the person's turn, and what it printed as a
  folded Output block.
- The pane reader (`server._prompt_parts`): a numbered menu's options are its numbered lines and the lines under them
  in the same column; its title (the line over its first option, or the line above that one when both start left of
  the options) is its question when no line with "?" is within three lines over it. The key line is the last hint
  line; the hint pattern also takes Codex's "enter select · esc back" and Claude Code's "←/→ to adjust".
- For an agent whose state comes from its pane (`STATE['state'] == 'detect'`: Codex), the pane read of the page wins
  over the blocked state that the watch of maxpm serve recorded (`sessions.pane_state`).

## As built in #5184 (setup and the page)

- `SETUP` holds `instructions`, `import` (the line with `{file}` by which the agent's instruction file imports
  another: Claude Code's `@{file}`), `skills_dir`, `voice`, `open_link` (the label of the page's button that opens a
  session's web link), `open_title` (its tooltip) and `no_link` (why a session that runs in no tmux pane here cannot
  open). `write_mcp_config(path=None, remove=False)` adds MaximizePM's MCP server (`agents.setup_entry()`) to the
  agent's config file, or takes it out, and returns what changed: `claude` writes the Claude desktop app's config,
  `codex` writes `~/.codex/config.toml` (`maxpm setup-agent --claude-desktop|--chatgpt-desktop`).
- `agents.SHARED_INSTRUCTIONS` is `AGENTS.md`, the one instruction file of a project folder. `agents.replaced_files()`
  gives each old file an agent read before (`SETUP["replaces"]`: CLAUDE.md), whose rules `maxpm init`, the setup guide
  and the Projects folder form move into AGENTS.md before they delete it.
- `GET /api/agents` (`agents.page_list()`) gives each agent's describe part, the parts it supports, its launch
  capabilities and its setup texts; `main` marks the first registered agent. The page reads it once
  (`static/components/agents.js`), and `views/sessions.js` tests no agent name. A session's `provider` has `agent`,
  the id of its adapter, and `voice_cli` comes from `SETUP["voice"]`.
## As built in #5179 (Amp)

- `river/agents/amp.py`, from Amp CLI 0.0.1794369644 on macOS. State comes from the screen (`STATE['state']` is
  `detect`), as the design names it for Amp. Amp has a plugin API with turn events and a thread state stream, but it
  loads plugins only from `~/.config/amp/plugins` (`$XDG_CONFIG_HOME/amp/plugins`) and a project's `.amp/plugins`:
  no flag loads one for a single start, a plugin there runs in every later Amp session of the person, and a start
  with its own `XDG_CONFIG_HOME` would move the config of every tool its shell commands run. The thread state stream
  also gave no `awaiting-approval` while an approval waited.
- `DETECT` reads the bottom edge of Amp's input box (an activity such as "≋ Running Tools" while it works, a plain
  rule when it waits) and its framed menus marked with ‣ (Approval Required, Out of Credits). Its `rule` also takes
  the lines Amp shows under an approval menu (the input box, "Up next (1)"), so the pane watcher finds the prompt.
- The session id is the thread id in `AMP_CURRENT_THREAD_ID`, which Amp sets for each shell command
  (`IDENTITY['source']` is `env`). Amp keeps the chat on its server: `chat_path` runs `amp threads export <id>` at
  most every 5 seconds and appends each finished message once to `amp/threads/<id>.jsonl` beside the queue.
- The effort is Amp's mode (`--mode low|medium|high|ultra`), which also picks the model, so DESCRIBE has no models. A
  first prompt goes on stdin (`printf '%s\n' <prompt> | amp`). Amp has no slash commands: "/" opens its command
  palette, and keys typed at once land in the wrong place, so the adapter has no COMMANDS.


## As built in #5185 (the old tables go)

- `core.LAUNCH_PLATFORMS`, `AGENT_FAMILIES`, `PLATFORM_EFFORTS`, `sessions.PLATFORMS` and `sessions.PROVIDERS` are
  gone. A platform name (the name that settings, launch profiles and `agents.agent_type` store) is the adapter's
  `DESCRIBE["platform"]` (Claude Code: `claude-code`), else its id; `core._platform_of` reads it, and `check` refuses
  a platform that is not the id or an alias. `core.launch_platforms()` and `core.platform_spec()` give what the table
  gave, for every registered agent.
- The built-in adapters' setting defaults go in `DEFAULT_SETTINGS` once, when core loads. A custom command's family
  and effort levels come from the agent whose program it runs (`agents.by_exe`). The order in which MaximizePM tests
  the environment of each agent is `agents.env_order()` (`ENV_LAST`: codex, then claude).
- A session's provider label and voice text come from its adapter; a session outside tmux moves into tmux when its
  agent has the resume part. Codex's model cache reader and flag reader (`codex_models`, `codex_flags`,
  `CODEX_FLAGS`) moved from core into `codex.py`. The hook file of a start is the hook file of that start's adapter.
- Kept on purpose: `core._PROFILE_MIGRATION` (the agents of the one-time change of launch_agents into profiles: fixed
  history), the Claude desktop and ChatGPT desktop writers of `maxpm setup-agent`, and the `root`/`croot` test
  folders of `sessions.transcript`. The setup guide offers every agent that MaximizePM can start, from the adapters
  (`server.known_agents`, #5225).

## Model and effort lists

The Start an agent dialog and a session's Settings dialog list each agent's models from one function,
`core.platform_models`, in one order (`agents.order_models`, #6256): the agent's own choice (auto) first, then each
provider and maker in the order the agent names them, newest generation first, then the strongest. Each model with
levels of its own carries them, and the Effort list shows only those; for "the agent's default" it shows the levels
that any of the agent's models takes. A list longer than 30 shows its picks and finds the rest with Other model….
What each agent's list came from on this Mac on 2026-11-20 (#6257):

| Agent | Source of the list | Models | Effort levels |
|---|---|---|---|
| Claude Code | `model_ladder` (fable, opus, sonnet, haiku) | 4 | the agent's: low to max |
| Codex | `model_ladder`, and Codex's model cache (`~/.codex/models_cache.json`, hidden models left out) with Codex's names | 10 | each model's, from the cache |
| Antigravity CLI | `agy models`, read by a thread (`agents.CliList`); one row per model, the level out of the id | 11 | each model's, from its ids; none for the Orchestrators |
| Pi | `pi --list-models` (the signed-in providers), Pi's `defaultProvider` first and offered as picks | 299 | Pi's levels for a model with thinking, none without |
| GitHub Copilot CLI | `DESCRIBE['models']`: Copilot prints no list | 8 | the agent's |
| Hermes | Hermes's model cache of its provider | 17 | the agent's |
| Grok CLI | Grok's `models_cache.json` | 1 | each model's, from the cache |
| OpenCode | `opencode models --standalone` | 778 (4 picks) | none: OpenCode takes no effort at the start |
| Goose | goose's model cache (`sessions.db`), else each provider's model in its `config.yaml` | 1 | the agent's |
| Aider | `DESCRIBE['models']`: Aider 0.86.2 prints its list only in a terminal, and knows no 2026 model | 7 | low, medium, high |
| Amp | none: Amp picks its model by its mode (the effort list) | 0: no Model field | low, medium, high, max (ultra) |
| Cursor CLI | `cursor-agent models` (284 ids), one row per model, the level out of the id | 79 (10 picks) | each model's, from its ids |

## Tier 1 agents

mark 2026-11-19: 'we should add it and have a definitive feature set'. The tier 1 agents are Claude Code, Codex and
Antigravity CLI: `agents.TIER1 = ("claude", "codex", "antigravity")` in `river/agents/__init__.py`, and
`agents.is_tier1(name)` for an id, an alias or a platform name. Each one has every feature below. The setup guide
shows the tier 1 agents first (Choose an agent); the other agents are under More agents and have the features their
adapter gives.

`tests/test_agent_tier1.py` checks each feature that code can check, for each tier 1 agent, and fails with the agent's
and the feature's name. A feature that a tier 1 agent lacks is an exception below, with its fix item or the reason
it cannot have it; the test fails when an exception no longer holds, and when this section does not name a feature
or an exception. The end-to-end table under it proves what only a real run proves (#6253).

| Feature | What a tier 1 agent has | Proof |
|---|---|---|
| `parts` | All the adapter parts: describe, settings, effort, launch, identity, chat log, state, screen rules, input, resume, commands, setup | unit |
| `launch` | `build_command` starts its program with the name, folder, model, effort, settings and first prompt; every start path uses it (launch, Dispatch, serve's fresh, operational, review and deployer starts, manager, expert) | unit; e2e Start |
| `resume` | `resume_command` resumes a session by its id: the Settings dialog's restart, expert resume and crash resume (`adopt_sessions`, `revive_sessions`, `session_restart`, `move_into_tmux`) | unit; e2e Resume |
| `identity` | MaximizePM learns the session's id, and a maxpm command knows that it runs inside the agent (`inside_env`) | unit |
| `chat` | The Sessions tab finds and reads its chat log: prompts, replies, tool calls and results | unit; e2e Chat |
| `states` | Working, idle and blocked, from its hooks or its screen; blocked includes the folder trust question and each approval menu | unit (the rules exist, one reads trust); e2e States |
| `ask_card` | Its own question dialog shows as a card on the page and is answered there (`DETECT['ask']`, a form of `river/ask_forms`) | unit; e2e Question card |
| `interrupt` | Stop on the page ends the running turn and keeps the session (`INPUT['interrupt']`) | unit; e2e Stop |
| `native_message` | A maxpm message reaches the session during a turn (`INPUT['native']`); the message box reaches its prompt | unit; e2e Message box |
| `commands` | The "/" list in the message box with Tab, its custom commands for the folder, and `/model` opens the Settings dialog | unit; e2e / list |
| `effort` | Its effort levels, the flag that sets one, and the Settings dialog row | unit |
| `model_order` | The model list of the launch dialog and the Settings dialog is newest generation first, then strongest first (`agents.order_models`), each model once with the name people read; the effort list shows only the levels that the agent and the chosen model take, low to high, and changes with the model (#6256) | unit; e2e Start |
| `settings_read` | The Settings dialog of a running session shows each value that the session runs with (model, effort, mode and each other setting), also when it started with none named: from its chat log (`chat_settings`), its screen (`screen_settings`: agy's footer) or the agent's own config (`config_settings`), as `SETTINGS_READ` declares; `tests/test_model_order.py` refuses a setting with no source for every agent (#6260, #6280) | unit; e2e Settings |
| `usage` | The context meter and the Usage panel (`context_window`), and `maxpm usage` (its token counts) | unit |
| `setup_files` | `maxpm init` writes its instruction file, and MaximizePM's MCP server goes into its config | unit |
| `setup_main` | Setup installs it, checks it (its status command) and signs it in (`agentinstall.METHODS`) | unit; e2e Setup |
| `host_signin` | `maxpm agent-login` and `maxpm host signin` sign it in on a computer with no browser (`agentlogin.AGENTS`) | unit |
| `roles` | `agent_for.<role>` (worker, manager, reviewer, deployer, expert), `default_agent`, `agent_rules` and `--agent` take it | unit; e2e Roles |
| `jobs` | `maxpm runner add/set` and `maxpm runner github/gitlab/pulumi` take `--cli <its program>` (`runners.CLIS`, `agent_job.SECRET`) | unit |
| `shell_guard` | A hook before each shell command, so MaximizePM refuses a `tmux kill-server` of its server (`core.tmux_guard`) and a push while an alert is unread (`core.push_guard`) | unit; e2e Shell guard |
| `secrets` | `maxpm secret` confirms the session from its process (`vault.AGENT_PROGRAMS`) | unit |
| `first_go` | Started by MaximizePM in any project folder, its first `maxpm go` and every later command run in that folder, and it claims its item with no typed help (mark 2026-11-19, #6278) | unit (agy: its start names no `--add-dir`, as agy runs a command in such a folder, not in its start folder); e2e First go, in a project folder that is not biggestriver |
| `read_dirs` | The project's setting `read_dirs` (folders, comma-separated): the session reads them with no question for each file, writes nothing in them, and never reads their secret files (.env, .env.*, .envrc, *.secret, *.secrets, a secrets folder) (mark 2026-11-20, #6281). Claude Code: `--add-dir` and `--disallowedTools` rules; Antigravity CLI: `--settings` allow `read_file(<folder>)`, deny `write_file(<folder>)` and `read_file(<file>)` for each secret file found at the start (its rules take paths, not patterns; never `--add-dir`, #6278); Codex (#6304): its sandbox reads the whole disk and writes only in its workspace, and each start and resume adds `-c default_permissions="maxpm_read_dirs"` and `-c permissions.maxpm_read_dirs={extends=...,filesystem={...}}` (extending `config.toml`'s `default_permissions` or `:workspace`, denying those secret globs) | unit; e2e 2026-11-20 (#6281, #6304, a fake .env outside /tmp): Claude Code, Antigravity CLI and Codex read the folder's Cargo.toml with no question and were refused its .env, and writes in the read folder were refused (Codex also ran its PreToolUse hook with no trust prompt). Claude Code asks once per start to add a folder it was never started in |

Exceptions (each also in `EXCEPTIONS` of the test):

- Antigravity CLI, `native_message`: agy 1.5.13 has no command, socket or flag that adds a message to a running
  session (its reference: only `-p`, `--conversation`, `acp` and `remote-control`). The message box types into its
  pane, which works at its prompt. With the setting `agy_activity_hooks` on (#6351), a maxpm message that comes
  during a turn reaches the session at the end of that turn: the Stop hook answers `{"decision": "continue",
  "reason": <the messages>}`, and agy gives the model the text as a system message.

Antigravity CLI's `shell_guard` is behind the setting `agy_activity_hooks` (off by default, mark 2026-11-20). agy
loads hooks only from `hooks.json` in its home or in the project, and an adapter installs nothing in a CLI's home
(p140); no flag adds a hook for one start. With the setting on, each start of an agy session (and `maxpm init`)
writes MaximizePM's hooks under the name `maximizepm` into the start folder's `.agents/hooks.json` (and the line
`/.agents/hooks.json` into the repository's `info/exclude`), and keeps the person's other hooks; off takes out only
that name. The PreToolUse hook of `run_command` runs `core.tmux_guard` and `core.push_guard` and answers `{"decision":
"deny", "reason": ...}`; PreInvocation reports working and Stop reports idle (blocked still comes from the screen: no
event says it). A hook in an agy session that MaximizePM did not start (no `MAXPM_AGENT` or session token in its
environment) does nothing. With the setting off, the tmux guard is deny rules of the session's `--settings` (#6251,
`antigravity.DENY_TMUX`), and nothing refuses a push.

Lists that stay as they are, as they are not a tier: `agents.ASK_FORMS` (the question forms that `river/ask_forms`
draws, also Copilot's and OpenCode's), `vault.AGENT_PROGRAMS` (the programs of every agent), and
`core._PROFILE_MIGRATION` (the agents of a one-time change, fixed history).

End-to-end runs of the tier 1 agents (#6253): a test serve on a queue of its own (`MAXPM_DB`) with its own tmux
server, driven through the page's API and the page itself, with its desktop and 390 px screenshots.

| Agent | Setup (install, check, sign-in) | Start, with its settings | First go (another project folder, no typed help) | Chat | Message box | States (working, idle, blocked, trust, approval) | Question card | / list | Stop | Resume after a stop | Roles (manager, reviewer) | Shell guard |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Claude Code | pass | pass | pass (the Dispatch runs) | pass | pass | pass | pass | pass | pass | pass | pass | pass |
| Codex | pass | pass | pass (the Dispatch runs) | pass | pass | pass; a typed "tell instead" answer is not sent (#6259) | fail: a question during a turn shows no card (#6258) | pass | pass | pass | pass | pass (#6254) |
| Antigravity CLI | pass | fail with an effort: the model is lost (#6260); pass without | fail before #6278 (its maxpm go ran in the queue folder, which --add-dir named); pass after it (no --add-dir) | pass | pass | pass; still working after a Stop (#6261) | pass | pass | pass | pass | pass | setting off: not reached, the model refused the command itself (#6251); `agy_activity_hooks` on (#6351, 2026-11-20, claude-sonnet-5-5 low in agy): a `git push` refused while an alert was unread, the alert brought in at the turn's end, working and idle from the hooks in the second of each |

The runs of 2026-11-19, with Claude Code 2.1.329 (haiku, low), codex-cli 0.159.1 (gpt-6-luna, low) and agy 1.5.13
(gemini-3.8-flash-medium). Each run: Dispatch of a worker item from the page (the agent asks the person a date format
with its own question tool, writes a line, commits and runs `maxpm done`), the trust question and an approval menu
answered on the page, a reply through the message box, `/mo` and Tab to `/model`, which opens the Settings dialog, a
Stop during a long reply, a restart with a changed setting (its chat goes on), then Start manager and a Dispatch of
the release's review item (the reviewer passes it). Shell guard: Claude Code and Codex (after #6254, in a second run)
refused a bare `tmux kill-server` with the guard's reason. agy (after #6251, with the deny rules in its start
command) did not call its tool: its model refused the command twice by itself, so the run did not reach the rule. Screenshots: Setup, the question card, the Settings dialog and the Sessions tab, at desktop width
and 390 px. Also found: the Settings dialog's row labels overlap their selects (#6262).

Open gaps, each with its fix item:

- Codex, question card: #6258. codex-cli 0.159.1 asks a question during a running turn as one line, '? 1 question',
  with 'shift+← to answer'; the page shows working and no card.
- Codex, an approval answered with "No, and tell Codex what to do differently" and a text: #6259. The text stays in
  Codex's input line until an Enter.
- Antigravity CLI, start with an effort: #6260. `--effort` with `--model` makes agy 1.5.13 start on its default model;
  its model ids carry the effort.
- Antigravity CLI, state after a Stop: #6261. The transcript's last step reads as working and wins over the screen,
  which shows idle; the Settings restart then refuses until another turn ends.

- Antigravity CLI, first `maxpm go` (#6278): each run started in a scratch project, not in biggestriver. Claude Code
  and Codex reached their item with only the trust card answered on the page. agy's first start ran `maxpm go` in
  the `--add-dir` folder (the queue's folder) and found no project; a second start, with the queue folder beside the
  project, reached its item.

## Tests in maxpm (step 5)

One row for each agent: its end-to-end test on this Mac, from a test serve on a queue of its own (`MAXPM_DB`) with
its own tmux server, driven through the page's API (`/api/action`) and the page itself.

| Agent (item) | Start from the page, with its settings | Chat on the Sessions page | Message box | State (working, idle, blocked) | / completion with Tab | Resume | Fix items |
|---|---|---|---|---|---|---|---|
| Pi 1.5.2 (#5187), openai-codex/gpt-6.1-sol | Works: `launch_agent` with model, effort `low` (`--thinking low`), Offline start on and `pi_args` `--provider openai-codex`; the state extension loads, Pi runs `maxpm go`, takes the item and closes it in 9 s. A `provider/model` text was refused then; #5236 fixed it. Pi needs it when several providers are signed in (a bare `gpt-6.1-sol` is ambiguous). | Fails: Pi's `CHAT` has no `ref`, so no log is found and the page says "No transcript". With `ref` `session` (checked in memory) the chat shows its 12 entries, the turn and the title. | Works: at its prompt Pi gets the text in 6 s; during its foreground `maxpm wait` the text waits ("Steering") until the wait returns after `wait_step` (9 min). | Works: `report:working` and `report:idle` in `--seq` order; `/quit` gives `report:gone` through `maxpm release`. Blocked: none, as designed (Pi has no permission questions). | Works: `/api/session-commands` gives its 25 built-in commands; on the page `/thi` and Tab give `/thinking `. Its folder is unknown without the chat log, so the folder's prompts and skills are missing. | Works: `core.resume_command` gives `pi -e <extension> --model … --session <id> --provider openai-codex`; Pi appends to the same session file, reports idle with the same session id, and remembers its earlier files. The Settings dialog restart refuses without the chat log. | #5255 (the chat and the tool rows), #5256 (a message during a foreground wait), #5257 (the "No transcript" note) |
| Hermes 0.22.7 (#5190), openai-codex/gpt-6.1-terra | Works, with one fault: Start an agent with model, effort `low` (`--reasoning low`) and Checkpoints on (`--checkpoints`) reaches `hermes chat --cli`; but its first `maxpm go` read the default queue, because its terminal tool gets no MaximizePM variable (#5241) | Works: prompts, tool calls, replies, and its own question as a card | Works: text, and the answer to its question, reach the pane | Works: working and idle; blocked at its `clarify` question, answered from the page. Its approval mode `smart` let `rm -rf` of a test folder run with no question | Works: `/reas` lists `/reasoning`, Tab completes it, and the command runs | A plain resume works (`--resume <id>`, context kept); a restart with a new effort fails: Hermes refuses `--reasoning` on resume (#5252) | #5241, #5252, #5253 (four small page faults), #5225 (no Add Hermes in the setup guide) |
| Amp 0.0.1794384042 (#5196), mode `low` | Works after two fixes: Start an agent with effort `low` (`--mode low`) and Thread visibility `private`; its three toggles show. The dialog has no field for a text setting, so `amp_settings_file` came from the config (an `ask shell_command` rule). The first start ran with no MaximizePM variable (#5239: before `printf <prompt> | amp` they reached `printf` only), then looked ended (#5246: the exited `printf` led the process group, so tmux named the pane zsh; Amp now gets `<<< <prompt>`). After both fixes Amp took the item and closed it. | Works: the prompt, the tool calls and Amp's replies (from the thread export) | Works: text from the page reaches its prompt (idle, then working), also during its foreground `maxpm wait` | Works: working and idle from the screen; blocked at each shell command under the `ask` rule, with the message 'Approval Required' (#5226), answered in the terminal. Before an `rm` it asked the person through the queue, as the guide says. The answer card stays empty before its first maxpm command (#5271) | None, as designed: no `/` list opens, and `/api/session-commands` gives none | Works: a Settings dialog restart with Fast mode on ran `amp … --fast … threads continue <thread>`, the same thread with its chat; the launch's `--visibility private` was dropped (#5274) | #5239, #5246, #5271, #5273 (an empty visibility choice), #5274, #5225 (no Add Amp in the setup guide) |
| GitHub Copilot CLI 1.0.109 (#5193), model auto | Works, with one fault: the launch dialog lists Copilot only after a `launch_agents` entry, as it lists only those entries (#5234); then model `auto` and effort `low` give `copilot --plugin-dir <queue>/copilot-plugin --name … --model auto --reasoning-effort low --add-dir <queue folder> -i …`. Permissions and Mode show a blank choice twice (#5235). | Works: the first prompt, tool calls, permission events, results and replies; a resumed chat shows a `resumed` divider. Before the first prompt Copilot writes no log, so the page shows no chat during the folder trust question. | Works: a message typed at its prompt reaches the pane, and the reply shows in the chat. | Works: the hooks report working (with `maxpm now`), idle, and blocked at each shell or file permission ("Run command: maxpm go"); the pane watcher reads the folder trust question; each question is answered from the page (Send answer). After a person's answer, serve ended the idle session and a fresh Copilot session took the item. | Works: `/mo` lists `/model`, `/move`, `/remote`, `/memory`; Tab gives `/model `. | A plain resume works: `copilot --resume <id> --model auto --reasoning-effort medium --add-dir …` keeps the context (it recalled its earlier reply). Restart with these settings fails: "the transcript does not name its folder" (#5266); the Settings dialog shows the effort as not known (#5258). | #5234, #5235, #5258, #5266, #5225 (no Add Copilot in the setup guide) |
| Grok CLI 1.0.77 (#5189), grok-4.7 on a free account | Works: Start an agent with Grok, model `grok-4.7`, effort `low` and Permission mode `default` gives `grok --agent <hooks file> -m grok-4.7 --reasoning-effort low --permission-mode default "<prompt>"`; after the trust question Grok runs `maxpm go`, takes the item, commits and asks before the push. A start whose tmux failed had left the session registered and the item pushed to it (#5263). | Fails: `grok.chat_path` returns a str, so `/api/sessions` and `/api/session` crash and the tab shows no session at all, of any agent (#5260). With a Path (checked by hand) the row, the chat (14 entries), the tool calls and the decision card show. | Works: the "Waits for you" answer and the message box both reach Grok; a question in the box got its reply in about 3 s. | Works: the hooks report working and idle (waits for an answer while its question is open), and the session id. Blocked: not checked live (the free usage limit ended the turns); the watcher finds none of Grok's blocked screens, trust, permission or usage limit, also not the recorded ones (#5261). | Works: `/mo` lists `/model` and `/memory`; Tab gives `/model `. | Works in the CLI: `grok --resume <id> -m grok-4.7 --reasoning-effort low --permission-mode default` brings back the whole chat. The Settings dialog shows model and effort as "not known", has no Grok setting, and its restart refuses: the log names no folder (#5262). | #5260, #5261, #5262, #5263 |
| Goose, Homebrew (#5195), ChatGPT sign-in, Tool approval `approve` | Works after #5234: the dialog listed only the `launch_agents` entries, so Goose was not offered; now every registered agent is. Start an agent with Goose and Tool approval `approve` gives `env GOOSE_MODE=approve goose run --interactive --name … --text <prompt>`; Goose runs `maxpm go`, takes the item, writes the file and closes the item. | Fails: no chat. The adapter takes the session id from the pane header (`IDENTITY` source `pane`), and no code reads that rule yet, so no session id is recorded and the page says so (#5268). | Works: a message typed in the box reaches its prompt and Goose answers. | Works, slowly under load: blocked at each tool approval (Goose's cliclack menu), with its question, answered from the page (Send answer, and Other: tell it what to do instead); idle at its prompt. Working was not seen: each turn was shorter than a pass of the watcher. Before its first maxpm command (an approval of `load_skill`) the row said 'not connected yet' (#5269, #5271). | Works: `/sta` lists `/status`; Tab gives `/status `. | Fails: a Settings dialog restart refuses with no transcript; the adapter's `goose session --resume --session-id <id>` needs the session id (#5268). | #5234 (done), #5268, #5269 |
| Aider 0.86.2 (#5194), `ollama_chat/qwen2.5:3b` on the local Ollama, no key | Works, with faults: Start an agent with Aider and Edit format `whole` gives `aider --edit-format whole --chat-history-file <queue>/aider/<start name>.chat.history.md --no-show-release-notes --set-env MAXPM_AIDER_SESSION=<start name>`. The dialog has no field for a model outside its list or for a text setting, so `--model ollama_chat/qwen2.5:3b` came from `aider_args` (project scope). Aider then stopped at two start questions, the `.gitignore` one and the model warning (OLLAMA_API_BASE not set) (#5270). After them maxpm pasted the first prompt (#5221), and Aider answered it. | Fails: no chat. The log has both turns (the adapter reads 8 entries), but no session id is recorded: Aider runs no maxpm command, and the start does not record the name it chose (#5280). The agent type was not recorded either; #5269 now records it at the start. | Works: `y` typed in the box answered the `.gitignore` question; `n`, a `/ask` prompt and `/exit` reached the pane through the same action (`session_send`). | Partly: the watcher finds the adapter by the pane's process (Python, #5223) and reports idle. Working was not seen: the spinner shows only until the first token, and each turn ended between two passes (#5277). Blocked: not found; in a 57-column pane the question's `:` goes to the next line (#5265). | Fails: `/api/session-commands` gave none without the agent type (#5269, done after this test). | From the page: refused, the Settings dialog knew no settings of the session's provider (#5269). The adapter's `resume_command` gives the same log with `--restore-chat-history` (checked in the CLI in #5177). End now was refused too: no recorded process (#5278); `/exit` from the box ended it. | #5265, #5270, #5277, #5278, #5280, #5269 (done), #5225 (no Add Aider in the setup guide) |
| OpenCode 2.2.7 (#5188), opencode/big-pickle (free, no key) | Works: Start an agent lists OpenCode, its free models first and Other model… for the rest, and its two toggles (Own server on, Approve permissions off); with model big-pickle it runs `env OPENCODE_CLI_CONFIG_CONTENT=<plugin> env OPENCODE_CONFIG_CONTENT={"model":…} opencode --standalone --prompt …` in a tmux pane; OpenCode runs `maxpm go`, writes the file, commits and closes the item. With no model chosen the launch leaves the model out, so OpenCode uses the model it remembers, not the adapter's default (#5284). | Fails, fixed in the test: no chat ("No transcript"), as `CHAT` had no `ref` and `chat_path` and `chat_chain` the old arguments (#5267). After the fix the chat shows the prompt, the replies and the tool calls; a failed turn's error shows once. | Works: the text reaches the pane; during its foreground `maxpm wait` OpenCode holds it until the wait ends, and since #5256 that is at once (it answered `ping` 2 s after the send). | Works: the plugin reports working and idle with the session id and the resume command, and blocked at the permission of a `.env` read with the message 'permission: read .env'. The card has no options: OpenCode's menu is one line (`Allow once   Always allow   Reject`), and the watcher reads only menus with one option per line (#5281); Reject was sent in the terminal, and the state went back to idle. | Works: `/com` lists `/compact`, Tab gives `/compact `. The folder's `.opencode/commands` were missing, as the chat file named no `cwd` (#5275, fixed in the test). | Partly: the Settings dialog restart resumes the same session (`opencode --standalone --session <id> --prompt …`) with its chat and context, but the model change did not apply, as a session keeps its own model (#5285); the dialog offered only Model (#5286, fixed in the test). The old TUI stayed alive in its pane: maxpm records OpenCode's private server as the session's process, not the TUI (#5283). | #5267 (done), #5275 (done), #5281, #5283, #5284, #5285, #5286 (done), #5225 (no Add OpenCode in the setup guide) |
| Cursor CLI 2026.11.11-043ef28 (#5192), model `auto` on a Free account | Works: the launch dialog offers Cursor with its models, five efforts (the suffix of the model id) and its settings (Run Everything, Auto-review, Sandbox, Mode, Trust the folder, Approve MCP servers); `launch_agent` with model `auto` gave `cursor-agent --model auto "<prompt>"` in a tmux pane. The first start in the folder stopped at Cursor's trust question and then at the menus of its shell commands (#5264, answered from the page); then it ran `maxpm go`, took its item, appended the date to a file and closed the item. A `launch` with no `--prompt` lets a waiting session take the item first. The Free plan runs only Auto, Grok 4.7 and Grok 4.6 with fixed settings: `grok-4.7` (Cursor's Low variant) is refused ('Model unavailable on Free'). | Works: the prompts, the replies and the shell commands with their command lines, from the agent-transcripts log; no times and no tool results, as the log has none. | Works: a message from the box reached its prompt, and `pong` came back within 2 s. | Works: idle at its input line, working while a turn runs, and blocked at 'Run this command?' (Not in allowlist: uname) with its four options; Run (once) from the page ran it, and the state went back to idle. The card's command field is empty: the command shows only in the screen lines. | Works: `/api/session-commands` gives its 40 built-in commands; on the page `/run-e` and Tab give `/run-everything `. The folder's own commands and skills are missing: the log names no folder (#5301). | Works, with faults: Restart with model `grok-4.7` from the Settings dialog ran `cursor-agent --resume <chat id> --model grok-4.7 …` in a new pane with the same chat (context 11.8%, as before). The dialog showed Model and Effort 'not known' and no Cursor setting (#5299). The new turn failed (the Free plan), so the session ran no maxpm command: a second restart was refused, 'the process … is not known' (#5300), and the message box typed into the old pane, where zsh ran the text as a command (#5298). | #5298, #5299, #5300, #5301, #5225 (no Add Cursor in the setup guide) |
| Antigravity CLI 1.5.13 (#6248), `gemini-3.8-flash-medium` on a Google AI Ultra account, a test queue with its own serve and tmux server | Works after one fix: Dispatch of the item with Antigravity CLI gave `agy --model … --add-dir <queue folder> --settings '{"permissions":{"allow":["command(maxpm)"]}}' -i '<prompt>'`. The first start had no `--settings`: agy's auto mode runs each command in its sandbox, which refused to run maxpm ('operation not permitted'); the allow rule with a BypassSandbox note in the first prompt fixed it. The first start in a folder asks 'Do you trust the contents of this project?', answered from the page. Then agy ran `maxpm go`, took the item and closed it with its commit. | Works: the prompts, the tool calls and their results, the replies, from `transcript_full.jsonl`; the session id from `ANTIGRAVITY_CONVERSATION_ID`. | Not tested in this run. | Works: blocked at its trust question and at its own question dialog (`ask_question`), answered from the page (the dialog's card took 'US (11/19/2026)'), working with its spinner, idle at its footer. A read outside the workspace ('Allow access to this file?') read as no state; added after the run. | Not tested in this run (its 47 built-in commands and the skills of its folders are in the adapter). | Works: a stop from the page reached its background `maxpm wait`, and the session woke and ended; Restart from the Settings dialog with another model ran `agy --conversation <id> …` in a new pane, with the whole chat and the new model. | none |

## End-to-end run of every agent (#6399)

mark 2026-11-20: 'we need to take up a larger test of the agents as antigravity turned out to be a lot of work to get
functional'. One run of each agent on 2026-11-20, on a test queue with its own serve and tmux server, started with
`env -i` (`scripts/agent_e2e/`): `maxpm launch --item` (Dispatch), then the queue shows each step. The run answers no
card of an agent: the auto mode classifier refused an answer to a trust card from the test session, so an agent that
asks a question before its first `maxpm go` stops there. Grok CLI is not tested: policy p19 forbids xAI models
(manager 2026-11-20). `AGENTS.md`: print mode in a subfolder of a repository, with a codeword in the root's and in
the subfolder's `AGENTS.md`.

| Agent | Launch, model on its command line | go, claim, done (with a commit) | wait | A message | Stop | Usage limits | AGENTS.md (root, subfolder) |
|---|---|---|---|---|---|---|---|
| OpenCode (opencode/big-pickle) | pass | pass: 27 s from the claim to done | pass | pass: native, 'pong' in 11 s | pass: ended in 3 s | no reader | pass |
| Pi (its default, gpt-6.1-sol, `--thinking low`) | pass | pass: committed 19 s after the claim, then asked 'Push?' (result_look ask_first) | ran wait, then ended its turn at the first 'No work yet' (#6422) | not reached | serve ended it as idle | no reader | pass |
| Goose (its default) | pass | pass: committed 17 s after the claim, then asked 'Push?' | as Pi (#6422) | not reached | serve ended it as idle | no reader | pass |
| Amp (`--mode low`) | pass | pass: committed 25 s after the claim, then asked 'Push?' | as Pi (#6422) | not reached | serve ended it as idle | no reader | pass |
| Claude Code (haiku, low) | pass | stops at its trust question in a new folder and in a worktree of a new repository (the real projects' folders are trusted) | not reached | not reached | pass: serve finished the stop when the session stayed idle | the probe skips Claude Code while a session of it runs; the real page reads it | pass |
| Codex (gpt-6-luna, low) | pass | stops at 'Trust this folder?', also in a MaximizePM worktree: Codex trusts the repository root (#6415) | not reached | not reached | pass, as Claude Code | pass: from its log | pass |
| Antigravity CLI (default; then claude-sonnet-5-5-low) | pass | first run: trust question in a new folder; worktree run: no question, then 'Individual quota reached' at its first prompt | not reached | not reached | pass, as Claude Code | pass: the limit stop limits the group 'Claude and GPT models'; the item was not started again on another agent (#6418) | not run (quota) |
| GitHub Copilot CLI (auto, low) | pass | new folder: trust question; worktree: no trust question, then 'Do you want to run this command?' for maxpm go (#6417) | not reached | not reached | pass, as Claude Code | no reader | pass |
| Cursor CLI (auto) | pass; `--effort low` is dropped for model auto | trust question, also in a worktree (#6416) | not reached | not reached | pass, as Claude Code | no reader | print mode refuses an untrusted folder without `--trust` |
| Hermes (gpt-6.1-terra via openai-codex, low) | pass: it has a model with the launch's provider | fail: its terminal tool gets no MaximizePM variable, so `maxpm go` reads the default queue (#5241; after this run, maxpm reads them from the session that Hermes stored, and a one-shot Hermes session's `maxpm go` registered on its test queue) | not reached | not delivered: the session never ran a maxpm command on its queue | not run | no reader | pass |
| Aider (ollama_chat/qwen2.5:3b) | pass | fail: Aider runs no shell command by itself, so it never runs maxpm go | not reached | not reached | not run | no reader | fail: no `--read` (#6420) |

Also found: after the person's answer, the fresh sessions for the Pi, Goose and Amp items ran Claude Code, not the
agent of the Dispatch (#6419); `maxpm launch --agent` takes only a label, not an id (#6421); a session with a token
gets briefing lines with `--as` and then a note on each such command (#6423). The rest of the run, after #6415 to
#6418: #6424, below.

### The second run, after the trust and permission fixes (#6424)

2026-11-20, 19:36 to 19:55 UTC, the same scripts and rules, each project with `workspace worktree` (a MaximizePM
worktree of a new git repository), `wait_step` 2m. Versions: codex-cli 0.171.0 (Homebrew updated it at 19:29 UTC,
after the fixes), cursor-agent 2026.11.19-f005e9d, GitHub Copilot CLI 1.0.114, agy 1.5.13. Each agent ran first with
its default settings; Copilot and Cursor ran a second time with the permission setting that a person picks in the
launch dialog, so the run reached the later steps.

| Agent | Settings | Start with no question | go, claim, done (with a commit) | wait | A message | Stop | AGENTS.md (root, subfolder) |
|---|---|---|---|---|---|---|---|
| Codex (gpt-6-luna, low) | default | fail: 'Trust this folder?' although the start passes `-c projects.<worktree>` and `-c projects.<repository root>.trust_level="trusted"`; 0.171.0 ignores that override, also in a plain new git folder, and has `--trust` only on `codex exec` (#6437) | not reached | not reached | not reached | pass: serve ended it at the card | pass (run 1) |
| Codex (gpt-6-luna, low), 19:56 UTC, after #6437 (0567040: one inline `-c projects={...}` table) | default | trust: pass; then 'Allow the maxpm MCP server to run tool "go"?': the person's `[mcp_servers.maxpm]` of `setup-agent --chatgpt-desktop` (main queue, chat mode) reaches the session, and its model calls that tool instead of the shell `maxpm` (#6443) | not reached | not reached | not reached | pass: serve ended it | as above |
| Cursor CLI (auto) | default | trust: pass (`--trust`, #6416); then 'Run this command?' for `maxpm go`: nothing in its start allows `Shell(maxpm)` (#6439) | not reached | not reached | not reached | pass: serve ended it | pass with `--trust` in print mode |
| Cursor CLI (auto) | Run Everything (`--force`) | pass | pass: 15 s from the claim to done | pass | pass: 'pong' in 10 s, through its `maxpm wait` | pass: it ended its turn at once; its row left when serve ended the idle session (4.4 min) | as above |
| GitHub Copilot CLI (auto, low) | default | pass: no trust question in the worktree, and `maxpm go` ran with no question (#6417) | claim: pass; then 'Do you want to create ok.txt?': with no Permissions setting Copilot asks before each change, as the setting says | not reached | not reached | pass: serve ended it | pass (run 1) |
| GitHub Copilot CLI (auto, low) | Permissions `allow-all-tools` | pass | pass: 34 s from the claim to done | pass | pass: 'pong' in 15 s, through its `maxpm wait`; the native channel needs Copilot's experimental mode, which the person turns on | pass, slowly: its model ran `maxpm stop` on itself (refused: a person's command), then sat at its prompt until serve ended it (4.6 min) | as above |
| Antigravity CLI (default, Gemini 4 Neon high) | default | pass: no trust question in the worktree, and its Gemini quota had reset (19:40 UTC) | pass: 4.5 min from the claim to done; two `maxpm done` with no proof were refused, the third had `--commit` and `--test` | pass | pass: read through its `maxpm wait` in 45 s, 'pong' in 112 s | pass: it ended its turn at the STOP of its wait; its row left when serve ended the idle session (4.9 min) | pass: both codewords in print mode |

With the defaults, only Antigravity CLI ran the whole session with no question. Copilot's question about a file
write is its Permissions setting at work, not a defect. Codex's trust question went away with #6437; open: #6443
(Codex sessions get the chat app's maxpm MCP server) and #6439 (Cursor's allow of maxpm). A stopped session that sits at its
prompt leaves the queue only after serve's idle end, about 4.5 minutes, also when it ended its turn at once.

## Slash commands on the page (#5224)

mark 2026-11-11: a slash command that opens a picker or a full-screen view in the terminal opens the page's own form
for the same thing, for every agent. An adapter's `COMMANDS` entry says it (`river/agents/__init__.py`, part 10):
`setting` opens the session's Settings dialog at that row (#5220), and `page` `usage` opens the Usage panel; typed
with no argument, nothing goes to the terminal. A command with an argument goes as typed.

- The Usage panel (`components/usagePanel.js`, `GET /api/session-usage`, `sessions.session_usage`) gives what
  `/usage`, `/context`, `/cost`, `/status` and `/stats` show: the last call's context against the model's window
  (the adapter's `context_window(model)`), the session's tokens and model calls (its `chat_requests`, as
  `maxpm usage` counts them, else its `chat_usage`), and the plan limits where the agent writes them to its log (its
  `chat_limits`). Where an agent shows its limits only on its screen, the panel says so and offers its terminal. The
  header of every session shows the context meter (`ctx 12%`); a click opens the panel. Dollars show nowhere: the
  `model_prices` setting has no output price.
- The page has no action for a worker session's new chat, clear, resume, fork or rename (a new chat would lose its
  item and its context): those commands stay as they run (`now`, or `terminal` for a picker).
- The other agents follow in #5289 (Pi, OpenCode, Grok), #5290 (Hermes, Cursor CLI), #5291 (GitHub Copilot CLI,
  Aider, Goose, Amp) and #6248 (Antigravity CLI).

| Agent | Command | On the page |
|---|---|---|
| Claude Code 2.1.322 | `/model`, `/effort` | Settings: Model, Effort (#5220) |
| Claude Code | `/usage`, `/context`, `/cost`, `/status` | the Usage panel; its plan limits show only on its screen (`/usage` in the terminal) |
| Claude Code | `/export` | its numbered menu, answered in the page's dialog |
| Claude Code | `/config`, `/permissions`, `/agents`, `/hooks`, `/mcp`, `/memory`, `/resume`, `/rewind`, `/skills`, `/theme`, `/doctor`, `/ide`, `/login`, `/logout`, and the other views of `_RUNS["terminal"]` | needs the terminal: sent, and the chat links to it |
| Claude Code | `/clear`, `/compact`, `/plan`, `/fork`, `/init` | run at once; the chat shows what they do |
| Codex 0.159.1 | `/model` | Settings: Model (#5220) |
| Codex | `/permissions` | Settings: Approval |
| Codex | `/status`, `/usage` | the Usage panel, with its 5-hour and weekly limits and credits from its log |
| Codex | `/review` | its numbered menu, answered in the page's dialog |
| Codex | `/resume`, `/export`, `/diff`, `/mention`, `/skills`, `/hooks`, `/mcp`, `/apps`, `/plugins`, `/experimental`, `/statusline`, `/theme`, `/voice`, `/ps`, `/logout` | needs the terminal |
| Codex | `/new`, `/clear`, `/fork`, `/rename`, `/compact`, `/plan` | run at once |
| Grok CLI 1.0.77 (#5289) | `/model`, `/effort` | Settings: Model, Effort (with no argument Grok lists the choices) |
| Grok CLI | `/session-info`, `/context`, `/usage` | the Usage panel; its window from the models cache (grok-4.7: 256k, as `/session-info` says); no plan limit in its log |
| Grok CLI | `/resume`, `/rewind`, `/fork`, `/dashboard`, `/hooks`, `/plugins`, `/skills`, `/mcps`, `/settings`, `/memory`, `/doctor`, `/view-plan`, `/login` | needs the terminal (full-screen pickers and views) |
| Grok CLI | `/new`, `/compact`, `/rename`, `/always-approve`, `/auto`, `/plan`, `/goal`, `/loop`, `/btw`, `/copy`, `/export`, `/remember`, `/quit` | run at once |
| Pi (#5289) | `/model`, `/thinking` | Settings: Model, Effort (pickers in Pi) |
| Pi | `/session` | the Usage panel; its window from `~/.pi/agent/models-store.json` by provider (openai-codex/gpt-6.1-sol: 500k, as its footer says) |
| Pi | `/settings`, `/tree`, `/scoped-models`, `/share`, `/hotkeys`, `/changelog`, `/fork`, `/trust`, `/login`, `/resume`, `/mcp` | needs the terminal |
| Pi | `/new`, `/compact`, `/clone`, `/name`, `/export`, `/import`, `/copy`, `/bug`, `/reload`, `/logout`, `/quit` | run at once |
| OpenCode (#5289) | `/models` | Settings: Model |
| OpenCode | `/usage`, `/stats` | the Usage panel (the session's tokens; its window from the models.dev catalog in its database: opencode/big-pickle 200k); its workspace limits need `/connect`, on its screen |
| OpenCode | `/status` (its servers), `/sessions`, `/resume`, `/continue`, `/agents`, `/themes`, `/settings`, `/effort`, `/thinking` (the model's variant), `/rename`, `/export` and its other dialogs | needs the terminal |
| OpenCode | `/new`, `/clear`, `/compact`, `/fork`, `/undo`, `/review`, `/goal`, `/btw`, `/copy`, `/reload`, `/restart`, `/update`, `/exit` | run at once |
| Hermes 0.22.7 (#5290) | `/model`, `/reasoning` | Settings: Model, Effort |
| Hermes | `/status`, `/context`, `/usage` | the Usage panel (the session's totals from its log; its window from `~/.hermes/context_length_cache.yaml`); Hermes logs no single call, so no meter |
| Hermes | `/history`, `/rollback`, `/agents`, `/queue`, `/sessions`, `/resume`, `/config`, `/approvals`, `/diff`, `/tools`, `/skills`, `/help`, `/version` | needs the terminal (it prints them into its scrollback, not its log) |
| Hermes | `/new`, `/clear`, `/retry`, `/undo`, `/compress`, `/branch`, `/title`, `/stop`, `/bg`, `/btw`, `/steer`, `/goal`, `/plan`, `/review`, `/fast`, `/yolo`, `/verbose`, `/init`, `/image`, `/copy`, `/quit` | run at once |
| Antigravity CLI 1.5.13 (#6248) | `/model`, `/effort` | Settings: Model, Effort (a restart: its `/model` also changes later sessions) |
| Antigravity CLI | `/context`, `/usage` | the Usage panel (the session's tokens from its transcript; its docs name no window size, so no meter) |
| Antigravity CLI | `/config`, `/resume`, `/rewind`, `/permissions`, `/hooks`, `/mcp`, `/plugin`, `/skills`, `/agents`, `/tasks`, `/diff`, `/help` and its other panels | needs the terminal |
| Cursor CLI 2026.11.11 (#5290) | `/model`, `/sandbox` | Settings: Model, Sandbox |
| Cursor CLI | `/config`, `/resume`, `/rewind`, `/about`, `/logs`, `/help`, `/mcp`, `/plugin`, `/team`, `/load-workspace` | needs the terminal; Cursor logs no token counts, so it has no Usage panel |
| Cursor CLI | the toggles (`/ask`, `/max-mode`, `/fast`, `/auto-review`, `/run-everything`, ...), `/clear`, `/fork`, `/summarize`, `/rename`, `/goal`, `/plan` | run at once |
| GitHub Copilot CLI (#5291) | `/model`, `/permissions` (a numbered menu) | Settings: Model, Permissions |
| GitHub Copilot CLI | `/context`, `/usage` | the Usage panel; its plan's premium requests ('1 / 200 AIC') show only on its screen |
| GitHub Copilot CLI | `/session`, `/resume`, `/config`, `/settings`, `/agent`, `/skills`, `/mcp`, `/plugin`, `/tasks`, `/rewind`, `/limits` and its other views | needs the terminal |
| GitHub Copilot CLI | `/new`, `/clear`, `/compact`, `/fork`, `/rename`, `/plan`, `/ask`, `/review`, `/copy`, `/exit`, ... | run at once |
| Aider (#5291; it waited at its `.gitignore` question here: from its descriptions) | `/model`, `/reasoning-effort` | Settings: Model, Effort |
| Aider | `/tokens` | the Usage panel |
| Aider | `/settings`, `/models`, `/ls`, `/map`, `/diff`, `/help`, `/editor`, `/editor-model`, `/weak-model` | needs the terminal (it prints them) |
| Aider | `/add`, `/drop`, `/ask`, `/code`, `/architect`, `/clear`, `/reset`, `/undo`, `/commit`, `/run`, `/test`, `/lint`, ... | run at once |
| goose (#5291) | `/model`, `/mode` (it asks which) | Settings: Model, Mode |
| goose | `/status` | the Usage panel (goose shows 'Context: 0 / 1050000 tokens' for gpt-5.6-terra on its screen) |
| goose | `/help`, `/skills`, `/prompts`, `/doctor`, `/r`, `/t` | needs the terminal |
| goose | `/compact`, `/clear`, `/new`, `/goal`, `/grind`, `/exit`, ... | run at once |
| Amp (#5291) | none | Amp has no slash commands ("/" opens its command palette, #5196); the Usage panel opens from the context meter |
