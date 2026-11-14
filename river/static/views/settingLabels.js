// Plain labels of the settings keys (#5850, the manager's review of #5848: 'plain labels with the current value and its
// control in one row'). The key itself shows small under its label. An agent program's options (claude_*, codex_*,
// ...) get '<program>: <option>' from PROGRAMS and OPTIONS; a key that is in no list gets its words, so a new key
// still shows.

export const PROGRAMS = { aider: "Aider", amp: "Amp", claude: "Claude Code", codex: "Codex", copilot: "Copilot",
  cursor: "Cursor", gemini: "Gemini CLI", goose: "Goose", grok: "Grok", hermes: "Hermes", opencode: "OpenCode", pi: "Pi" };

const OPTIONS = { args: "more arguments", prompt: "first prompt", model_ids: "model names", permission_mode: "permission mode",
  permissions: "permissions", sandbox: "sandbox", approval: "approval", approval_mode: "approval mode", mode: "mode",
  remote_control: "Remote Control", activity_hooks: "activity hooks", skill_prompt: "skill prompt", skip_trust: "skip the trust question",
  trust: "trust the folder", auto_review: "review its own changes", approve_mcps: "approve MCP servers", force: "run commands without asking",
  yes_always: "say yes to every question", yolo: "run everything without asking", auto_commits: "commit by itself",
  analytics: "analytics", cache_prompts: "prompt caching", edit_format: "edit format", gitignore: "add to .gitignore",
  model_warnings: "model warnings", weak_model: "small model", fast: "fast mode", no_ide: "no IDE", quiet: "quiet output",
  settings_file: "settings file", visibility: "visibility", builtins: "built-in tools", max_turns: "most turns",
  checkpoints: "checkpoints", provider: "model provider", skills: "skills", toolsets: "tool sets", tools: "tools",
  offline: "offline", standalone: "standalone", auto: "automatic mode" };

export const LABELS = {
  // Agents
  launch_agents: "Agents the Start button offers", default_agent: "Agent program for new items", agent_rules: "Agent type by kind of item",
  default_model: "Model for new items", default_effort: "Effort for new items", default_min_model: "Weakest model allowed",
  default_max_model: "Strongest model allowed", effort_levels: "Effort levels", model_ladder: "Models, weakest to strongest",
  model_prices: "Model prices", prompt_pattern: "How a waiting prompt looks", prompt_wait: "Wait before a prompt counts",
  "agent_for.worker": "Agent for workers", "agent_for.manager": "Agent for the manager", "agent_for.reviewer": "Agent for reviews",
  "agent_for.deployer": "Agent for deploys", "agent_for.expert": "Agent for experts", "agent_for.context": "Agent for context sessions",
  // Sessions
  lease_ttl: "How long a claim lasts", hold_ttl: "How long a held item stays held", goal_lease: "How long a goal owner keeps its goal",
  goal_max_leases: "Goal items one session may hold", goal_context: "Context sessions for goals", base_warm: "How long a context base stays warm",
  base_max: "Largest context base", max_leases: "Items one session may hold", max_sessions: "Most sessions at once",
  busy_max: "Longest busy time without a command", idle_after: "Idle after", idle_end: "End an idle session after",
  wait_max: "End a waiting session after", wait_step: "One wait lasts", wait_too_long: "Waiting too long after",
  gone_after: "Gone after", away_after: "Away after", owner_ttl: "How long a target owner keeps its target",
  reserve_ttl: "How long a pushed item stays reserved", auto_continue: "After a task, take", fresh_sessions: "Start a fresh session after an answer",
  revive_sessions: "Resume a session that crashed", revive_within: "Resume only within", native_message: "Send messages into sessions",
  human_wait_max: "Longest hold on a person's answer", tmux_adopt: "Move sessions into tmux", shells_every: "Read session commands every",
  expert_idle_end: "End a quiet expert after", expert_fresh_min: "Offer a fresh expert from",
  session_idle_end: "End a quiet session with no item after", queue: "Use the queue",
  manage_every: "Manager looks every", manage_settle: "Manager waits for more findings", manage_wait_high: "Wake the manager for a high message after",
  manage_wait_low: "Wake the manager for a low message after", manage_rewake: "Wake the manager again for an open message after",
  manage_unanswered: "An unanswered message is a finding after", manager_autocompact: "Manager context compacts at",
  question_hold: "The manager sees questions first for", question_nudge_after: "Remind about an open question after",
  question_popup_max: "Open 'Waits for you' by itself at most", question_wait_end: "End a session that waits for an answer after",
  web_terminal: "Terminal tab", web_terminal_relay: "Terminal tab through the relay",
  // Work
  review: "Review before release", review_cmd: "Review command", review_prompt: "Review prompt", review_timeout: "Stop a review command after",
  auto_review: "Start reviews by itself", release_approval: "Release gate", release_commits: "Command that lists a release's commits",
  deploy_timeout: "Stop a deploy after", hook_timeout: "Stop a hook after", result_look: "A finished result is looked at",
  tracker: "Outside tracker", push_guard: "Commands held while an alert is unread", due_warn_before: "Warn before a due date",
  replan_threshold: "Replan after this many new prerequisites", keep_prereq_limit: "Most prerequisites kept by the holder",
  default_prerequisite_mode: "A new prerequisite does to its item", stale_after: "An unclaimed item is stale after",
  unpushed_after: "Unpushed commits are a finding after", conflict_max_lines: "Shared files conflict up to this many lines",
  similar_min: "Same-work score", similar_days: "Look for the same work in the last", list_cleanup_after: "Clean up a person's list after",
  list_cleanup_min: "Clean up a person's list from",
  // Monitoring
  health_person_minutes: "Person minutes per item that needs you", activity_silence: "Silence before a session counts as stopped",
  ops_confirm: "Run a failed check again before a fault", ops_idle_work: "Idle work probe", ops_serve_late: "serve is late after",
  ops_note_every: "Add a note to an open fault every", ops_max_investigations: "Most fault investigations at once",
  ops_repair_max: "Most repairs of one check", ops_repair_window: "Repair count window", ops_noise_max: "Longest noise rule",
  ops_release_window: "A fault belongs to a release within", ops_watch_every: "Watch after a release every",
  ops_watch_cron: "Scheduled checks in the release watch", ops_proof_timeout: "Stop a proof command after",
  ops_maps_timeout: "Stop a source map build after", ops_fix_max: "Stop after this many fixes that did not clear a fault",
  ops_reopen_within: "Reopen a fixed fault within", ops_storm_count: "Faults that make a storm", ops_storm_window: "Storm window",
  ops_lease_loop: "Lease loop check", ops_release_loop: "Release loop check", ops_deadman_url: "Outside heartbeat address",
  ops_deadman_every: "Send the heartbeat every", ops_outside_repair_max: "Most watchdog restarts", ops_outside_repair_window: "Watchdog restart window",
  // Browser
  browser_allow: "Private addresses the browser may open", browser_accounts: "Accounts agents may use", browser_source: "Where the browser comes from",
  browser_lock: "Command that takes the browser", browser_unlock: "Command that gives the browser back", browser_mode: "Command that restarts an attached browser",
  browser_mcp: "Playwright tools for sessions", browser_pace: "How fast a session moves", browser_idle_end: "Close an unused browser after",
  browser_take_after: "A session takes the browser from you after", browser_window_idle: "A browser window goes back into the panel after",
  browser_shot_cursor: "Show the pointer in screenshots", canvas_max: "Largest canvas", canvas_versions: "Canvas versions kept",
  canvas_network: "What a canvas may load",
  // Notifications
  notify_channels: "Channels", notify_interval: "Check for notifications every", notify_batch_window: "Group notifications within",
  notify_open: "A tap opens", notify_status: "Push when a session enters", ntfy_url: "ntfy server", ntfy_topic: "ntfy topic", ntfy_token: "ntfy token", ntfy_priority: "ntfy priority",
  ntfy_click: "Phone link when no relay", timezone: "Time zone",
  // Advanced
  serve_port: "Port of the page", serve_reload: "Restart serve after a code change", serve_reload_min: "Least time between restarts",
  serve_runners: "Fast commands (runners)", serve_loop_process: "Run the loop in its own process", tidy_every: "Close finished panes every",
  update_check: "Look for a new version", relay_keep_awake: "Keep this computer awake for the relay", connect_within: "A started session must connect within",
  setup_done: "Setup guide finished", cache_ttl: "Prompt cache lasts", sample_every: "Record session states every", sample_keep: "Keep session states for",
  db_max_size: "Largest queue file", folder_max_size: "Largest queue folder", archive_after: "Archive closed items after",
  archive_max_size: "Largest archive", space_ops_after: "Keep check runs at least", space_notices_after: "Keep notices at least",
  space_events_after: "Keep history at least", space_canvas_after: "Keep old canvas versions at least",
  space_sessions_after: "Keep session records at least", space_files_after: "Keep uploaded files at least",
};

// The label of a key: its own, '<program>: <option>' for an agent program's option, else its words.
export function labelOf(key) {
  if (LABELS[key]) return LABELS[key];
  const m = key.match(/^([a-z]+)_(.+)$/);
  if (m && PROGRAMS[m[1]]) return `${PROGRAMS[m[1]]}: ${OPTIONS[m[2]] || m[2].replace(/_/g, " ")}`;
  const w = key.replace(/[_.]/g, " ");
  return w.charAt(0).toUpperCase() + w.slice(1);
}

// The program of an agent option key, for the subheadings of the Agents tab; null for a general key.
export function programOf(key) {
  const m = key.match(/^([a-z]+)_/);
  return m && PROGRAMS[m[1]] ? PROGRAMS[m[1]] : null;
}
