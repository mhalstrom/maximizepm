# Agent adapters: one interface for every coding agent

Decided by mark, 2026-11-11 ("1-3 are good"):

1. tmux stays the one terminal layer. Every agent runs its real CLI in a tmux window. There is no second backend and no fallback path (mark: "I don't want to have a fallback"). ACP, codex app-server and the Claude Agent SDK were rejected for now: Claude Code reaches them only through adapters that do not accept a claude.ai plan login, and the person loses the terminal. herdr was looked at: its ideas are copied (agent detection as data, one state report per agent), not the program.
2. Every agent is one adapter. The adapters replace all platform tables and all name checks.
3. WSL is the supported way to run maxpm on Windows (separate item).

Multi-provider models (OpenCode, Pi, Goose and others): option A. The model of such an agent is a free "provider/model" text. An item's min-model and max-model limits apply only to the families maxpm knows (claude, openai).

## Today (map of 2026-11-11, paths in river/)

- Four platform tables with no shared key: `core.LAUNCH_PLATFORMS` (core.py:347), `core.AGENT_FAMILIES` and `PLATFORM_EFFORTS` (core.py:11366), `sessions.PLATFORMS` and `sessions.PROVIDERS` (sessions.py:28, :39), plus the `{"claude": "claude-code", "codex": "codex"}` map copied at core.py:1505, :1526, :10750, :11233.
- About 30 functions test a platform name: `build_command` (core.py:10674), `_launch_agent_cmd` (:11217), `profile_from_command` (:10739), `resume_command` (:11294), `agent_type_from_env` (:1809), `fork_base` (:8931), `wait_background` (:11573), `start_expert` resume (server.py:352, :419), `move_into_tmux` (server.py:1164), the parsers `_claude`/`_codex` (sessions.py:297, :357), `transcript_chain`, `custom_title`, `_transcript_settings` (sessions.py:1237), `_cache_line`, `_line_hits`, usage measurement (core.py:8580-8760), the hook parser `activity_event` (core.py:8446), `background_work` (:8624), pane wording in `prompt_pattern` (core.py:183) and `_prompt_parts`/`_prompt_found` (server.py:1378-1470), the MCP config writers (cli.py:1382-1504), the instruction file names (cli.py:1944-2046), and the JS name checks in views/sessions.js:42-44, :82, :123.
- Effort levels exist in four copies (the `effort_levels` default, `PLATFORM_EFFORTS`, sessions.py:45, Codex's models cache), and the flag forms, the resume regex (core.py:11336) and the live `/effort` command are Claude- or Codex-only.
- `default_agent`, `agent_rules` and an item's `--agent` accept only `claude-code` or `codex` (`_agent_platform`, core.py:1503). No role has its own agent setting. `fresh_expert` takes the first entry; expert and crash resume work only for Claude Code and Codex.
- Grok, OpenCode, Gemini and Pi exist only as plain custom commands in `server.KNOWN_AGENTS` (server.py:2822): no agent type, no settings, no chat on the page, no resume.

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

What each agent gives for state (herdr's integration docs, 2026-11-11): Claude Code, hooks for many events (maxpm uses them now); OpenCode, a JS plugin with full state and session id; Pi, a TS extension with full state; Codex, Copilot CLI, Cursor CLI, Grok CLI, hooks that give the session id, state from the screen; Hermes, a plugin that gives the session id, state from the screen; Gemini CLI and Amp, screen only; Goose and Aider, not checked.

Each part below is optional unless marked required; the page shows only what an adapter supports.

1. Describe (required): id, label, exe, aliases, model family (or "multi" for provider/model text), model ids and default, an optional model list reader (Codex: `~/.codex/models_cache.json`).
2. Settings: a list of setting specs. Each spec: key, kind (toggle, choice, text), choices, default, how it goes on the command line, and how it changes in a running session (a live command such as `/model {value}`, or a restart). The Setup section, the launch dialog and the Settings dialog draw from these specs. Config keys stay `<prefix><key>`.
3. Effort: the agent's levels (static, or read from the CLI per model), a map from maxpm's levels (low, medium, high, xhigh, max) to the agent's names, the flag form, a live command or none, and how to read the effort back from the chat log. An agent with no effort gives none, and the page hides effort for it.
4. Launch (required): `build_command(name, folder, model, effort, settings, prompt)` returns the argv. Capabilities: session name, system prompt file, hooks file, extra dirs, fork. `parse_command(argv)` reads an old command back into a profile.
5. Session identity: how maxpm learns the session's id after the start (an env var, a new log file in a known folder, or text in the pane), its web link if any, and the env vars that tell maxpm a command runs inside this agent (replaces `agent_type_from_env`).
6. Chat log: where the log of a session is (by session id or address, compressed or not), `parse(lines)` to the shared entries (assistant, user with images, tool, result, divider, summary, turn state), and optionally: the chain of files across resumes, the title, the cwd, usage and cache, the settings read back, the tool names that write files (for Artifacts).
7. State: one declared source per signal, as above: a hook installer whose hooks call `maxpm report`, or the `DETECT` dict (replaces the wording in `prompt_pattern` and the Claude layout code in `_prompt_parts`). Claude Code's hooks move from `maxpm activity` to `maxpm report` in step 1, with no change of behaviour. Also: whether the agent waits for background commands in the foreground.
8. Input: tmux paste is the same for every agent. The adapter gives the image paste convention (Claude: "[Image #N]"), the text of the "tell it instead" option of a permission menu, and an optional native message command (Codex: `codex queue --thread`).
9. Resume: `resume_command(session_ref, model, effort, settings)` or none.
10. Commands (slash command completion, mark 2026-11-11): the agent's built-in slash commands, and a reader for its custom ones for a session folder (Claude: ~/.claude/commands, .claude/commands, the skills in ~/.claude/skills and .claude/skills, plugin commands; Codex: ~/.codex/prompts). Each entry: name, description, argument hint. The page: when the message box starts with "/", a list of matching commands opens above it; Tab completes; Up and Down move in the list while it is open (otherwise they recall messages as now); Esc closes it; the text is sent as now.
11. Setup: the instruction file name (CLAUDE.md, AGENTS.md, GEMINI.md, ...), the skills folder, the MCP config writer, the voice text, the open-link text.

## Agent choice for each role

A new setting `agent_for.<role>` for the roles worker, manager, reviewer, deployer, expert and context, at the global, project and item scopes, falling back to `default_agent`. `default_agent`, `agent_rules`, an item's `--agent`, and every start path (launch, Dispatch, serve's fresh, operational, review and deployer starts, manager, expert and fresh_expert, auto_context, deploy_now) take any registered adapter. Expert resume and crash resume (adopt_sessions, revive_sessions, session_restart, move_into_tmux) go through the adapter's `resume_command`.

## Work, in order

1. Claude Code and Codex become adapters with no change of behaviour; every name check above becomes an adapter call; all four tables and the copied maps go. The whole test suite stays green. This design file goes into the repo as docs/agent-adapters.md in this step.
2. Agent choice is generic: `agent_for.<role>`, `default_agent`, `agent_rules`, `--agent`, expert and crash resume through the adapter.
3. Settings, effort and slash commands come from the adapter: the Setup section, the launch dialog, the Settings dialog of a running session, and the "/" list with Tab in the message box (Claude Code and Codex lists first).
4. One adapter per new agent, in parallel after step 3: Pi, OpenCode, Grok CLI, Hermes, Gemini CLI, Cursor CLI, GitHub Copilot CLI, Aider, Goose, Amp. Each worker installs the CLI, reads its real flags, log files and hook or plugin system on this Mac, and writes the adapter: describe, settings, effort, launch, identity, state first; then the chat log; then resume and commands. herdr's detection files (Apache-2.0, github.com/herdrdev/herdr) may serve as a start for prompt patterns.
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
     CLI, #5173 Hermes, #5174 Gemini CLI, #5175 Cursor CLI, #5176 GitHub Copilot CLI, #5177 Aider, #5178 Goose,
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
   #5187 Pi (after #5170), #5188 OpenCode (#5171), #5189 Grok CLI (#5172), #5190 Hermes (#5173), #5191 Gemini CLI
   (#5174), #5192 Cursor CLI (#5175), #5193 GitHub Copilot CLI (#5176), #5194 Aider (#5177), #5195 Goose (#5178),
   #5196 Amp (#5179).

Separate: #5180 makes WSL 2 the supported way to run maxpm on Windows (decision 3 above); it waits on none of these.

## As built in #5167

- An adapter is a module; its parts are module-level names: `DESCRIBE` (+ `read_models`), `SETTINGS`, `EFFORT`
  (+ `read_effort_levels`, `read_effort`), `build_command` with `LAUNCH` (+ `parse_command`), `IDENTITY`,
  `chat_path` with `parse_chat` and `CHAT` (+ `chat_chain`, `chat_title`, `chat_cwd`, `chat_usage`, `chat_settings`),
  `STATE` with `DETECT` or `install_hooks`, `INPUT`, `resume_command`, `COMMANDS` (+ `read_commands`), `SETUP`
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
  `fork_base`, the `wait_background` of the go briefing, the effort check of a start, and the web link of an agent
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
  `native_message` (Codex's `INPUT['native']`); a test checks that they match the adapters.

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
  `core.measure_usage` and the size of a goal base). `chat_path` takes `root=` (a log folder that a test gives), and
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

- `agent_for.<role>` is a setting for each role of `core.AGENT_ROLES` (worker, manager, reviewer, deployer, expert,
  context). In each scope, from the most specific (item, kind, project, global), `agent_for.<role>` comes before
  `default_agent`: a project's `default_agent` wins over a global `agent_for.<role>`. An item's own `--agent` and
  `agent_rules` come first. The role of an item is `core.item_role`: reviewer for a review, deployer for a deploy
  item, expert for an expert-tier task, else worker.
- An agent type is any registered adapter with a launch part (`core.agent_types()`): `default_agent`,
  `agent_for.<role>`, `agent_rules`, an item's `--agent`, `MAXPM_AGENT_TYPE` and a `@<id>` profile in
  `launch_agents` take it. `core.platform_spec(type)` gives its label, exe, family, prefix and options (Claude Code
  and Codex: `LAUNCH_PLATFORMS` until #5185; another adapter: its DESCRIBE and SETTINGS, plus a first prompt).
- `core.launch_entries` is `launch_agents` plus one implicit profile (`<label>=@<id>`) for each agent type that no
  entry runs, so an agent starts with no `launch_agents` entry. The first entry stays the default start.
- `core.role_agent(conn, role, project_id, item_id)` gives the launch entry of a role. The manager, an expert, a
  context session and the focused starts (deploy, review, help, unblock) use it; launch, Dispatch, serve's fresh
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

## As built in #5184 (setup and the page)

- `SETUP` holds `instructions`, `import` (the line with `{file}` by which the agent's instruction file imports
  another: Claude Code's `@{file}`), `skills_dir`, `voice`, `open_link` (the label of the page's button that opens a
  session's web link), `open_title` (its tooltip) and `no_link` (why a session that runs in no tmux pane here cannot
  open). `write_mcp_config(path=None, remove=False)` adds MaximizePM's MCP server (`agents.setup_entry()`) to the
  agent's config file, or takes it out, and returns what changed: `claude` writes the Claude desktop app's config,
  `codex` writes `~/.codex/config.toml` (`maxpm setup-agent --claude-desktop|--chatgpt-desktop`).
- `agents.SHARED_INSTRUCTIONS` is `AGENTS.md`. `agents.instruction_files()` gives the file of the agent that imports
  it (CLAUDE.md), and `maxpm init`, the setup guide and the Projects folder form take the names from there.
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
  history), `server.KNOWN_AGENTS` (the commands that the setup guide offers), the Claude desktop and ChatGPT desktop
  writers of `maxpm setup-agent`, and the `root`/`croot` test folders of `sessions.transcript`.

## Tests in maxpm (step 5)

One row for each agent: its end-to-end test on this Mac, from a test serve on a queue of its own (`MAXPM_DB`) with
its own tmux server, driven through the page's API (`/api/action`) and the page itself.

| Agent (item) | Start from the page, with its settings | Chat on the Sessions page | Message box | State (working, idle, blocked) | / completion with Tab | Resume | Fix items |
|---|---|---|---|---|---|---|---|
| Pi 1.5.2 (#5187), openai-codex/gpt-6.1-sol | Works: `launch_agent` with model, effort `low` (`--thinking low`), Offline start on and `pi_args` `--provider openai-codex`; the state extension loads, Pi runs `maxpm go`, takes the item and closes it in 9 s. A `provider/model` text was refused then; #5236 fixed it. Pi needs it when several providers are signed in (a bare `gpt-6.1-sol` is ambiguous). | Fails: Pi's `CHAT` has no `ref`, so no log is found and the page says "No transcript". With `ref` `session` (checked in memory) the chat shows its 12 entries, the turn and the title. | Works: at its prompt Pi gets the text in 6 s; during its foreground `maxpm wait` the text waits ("Steering") until the wait returns after `wait_step` (9 min). | Works: `report:working` and `report:idle` in `--seq` order; `/quit` gives `report:gone` through `maxpm release`. Blocked: none, as designed (Pi has no permission questions). | Works: `/api/session-commands` gives its 25 built-in commands; on the page `/thi` and Tab give `/thinking `. Its folder is unknown without the chat log, so the folder's prompts and skills are missing. | Works: `core.resume_command` gives `pi -e <extension> --model … --session <id> --provider openai-codex`; Pi appends to the same session file, reports idle with the same session id, and remembers its earlier files. The Settings dialog restart refuses without the chat log. | #5255 (the chat and the tool rows), #5256 (a message during a foreground wait), #5257 (the "No transcript" note) |
| Hermes 0.22.7 (#5190), openai-codex/gpt-6.1-terra | Works, with one fault: Start an agent with model, effort `low` (`--reasoning low`) and Checkpoints on (`--checkpoints`) reaches `hermes chat --cli`; but its first `maxpm go` read the default queue, because its terminal tool gets no MaximizePM variable (#5241) | Works: prompts, tool calls, replies, and its own question as a card | Works: text, and the answer to its question, reach the pane | Works: working and idle; blocked at its `clarify` question, answered from the page. Its approval mode `smart` let `rm -rf` of a test folder run with no question | Works: `/reas` lists `/reasoning`, Tab completes it, and the command runs | A plain resume works (`--resume <id>`, context kept); a restart with a new effort fails: Hermes refuses `--reasoning` on resume (#5252) | #5241, #5252, #5253 (four small page faults), #5225 (no Add Hermes in the setup guide) |
