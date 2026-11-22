# Settings audit

Audit of every key in `river/core.py` `DEFAULT_SETTINGS` (#6866).

## Summary

- Total keys in `DEFAULT_SETTINGS`: **282** (189 built-in + 93 agent-adapter option keys).
- **REMOVE (no reader)**: **0** — every key in `DEFAULT_SETTINGS` has a live code reader that changes behavior. `tests/test_settings_audit.py` enforces this so an unused setting cannot be added.
- **NORMAL**: **15** common settings shown on the normal tabs of the Settings dialog and Settings page.
- **FOLD (proposed)**: **7** implemented settings that duplicate another setting or whose only sensible value is the default (kept in Advanced until approved).
- **ADVANCED**: **260** implemented, rarely changed settings shown only inside the closed, searchable Advanced section.

## All settings

| Key | Label | What reads it (`file::function`) | Saved today | Verdict |
|---|---|---|---|---|
| `lease_ttl` | How long a claim lasts | `river/core.py::_lease_for`, `river/core.py::still_worked` | yes | ADVANCED |
| `hold_ttl` | How long a held item stays held | `river/core.py::_hold_until` | no | ADVANCED |
| `goal_lease` | How long a goal owner keeps its goal | `river/core.py::_goal_lease`, `river/core.py::goal_give` | no | ADVANCED |
| `human_wait_max` | Longest hold on a person's answer | `river/core.py::_human_wait_ended`, `river/core.py::_go`, `river/core.py::item_add` | no | ADVANCED |
| `owner_ttl` | How long a target owner keeps its target | `river/core.py::target_handoff`, `river/core.py::_renew`, `river/core.py::target_own` | no | ADVANCED |
| `reserve_ttl` | How long a pushed item stays reserved | `river/core.py::push`, `river/core.py::_want_fresh`, `river/core.py::accept_message` | no | ADVANCED |
| `keep_prereq_limit` | Most prerequisites kept by the holder | `river/core.py::_prereq_mode`, `river/core.py::keep` | no | ADVANCED |
| `conflict_max_lines` | Shared files conflict up to this many lines | `river/core.py::_sync_conflicts` | no | ADVANCED |
| `similar_min` | Same-work score | `river/core.py::similar_items`, `river/core.py::_close_check`, `river/core.py::duplicates` | no | ADVANCED |
| `similar_days` | Look for the same work in the last | `river/core.py::similar_items` | no | ADVANCED |
| `replan_threshold` | Replan after this many new prerequisites | `river/core.py::_count_late` | no | ADVANCED |
| `default_prerequisite_mode` | A new prerequisite does to its item | `river/core.py::_prereq_mode` | no | ADVANCED |
| `max_leases` | Items one session may hold | `river/core.py::_lease_room` | yes | ADVANCED |
| `goal_max_leases` | Goal items one session may hold | `river/core.py::_lease_room` | no | ADVANCED |
| `away_after` | Away after | `river/core.py::_live_state`, `river/server.py::_done_panes` | no | ADVANCED |
| `gone_after` | Gone after | `river/core.py::_live_state` | no | ADVANCED |
| `question_nudge_after` | Remind about an open question after | `river/core.py::unread`, `river/core.py::_question_nudges` | no | ADVANCED |
| `serve_port` | Port of the page | `river/cli.py::_run`, `river/core.py::serve_port_now`, `river/login.py::_install_systemd` | no | ADVANCED |
| `notify_channels` | Channels | `river/core.py::_open_event`, `river/core.py::device_pair`, `river/notify.py::setup_ntfy` | yes | NORMAL |
| `notifications` |  | `river/cli.py::dispatch`, `river/core.py::notices_on`, `river/core.py::state` | yes | NORMAL |
| `notify_interval` | Check for notifications every | `river/cli.py::dispatch`, `river/core.py::_probe_serve`, `river/core.py::serve_loop` | no | ADVANCED |
| `notify_batch_window` | Group notifications within | `river/notify.py::status`, `river/notify.py::run` | no | ADVANCED |
| `tailnet_logins` |  | `river/tailnet.py::allowed_logins` | no | ADVANCED |
| `ntfy_url` | ntfy server | `river/notify.py::setup_ntfy`, `river/notify.py::_ntfy_channel` | no | ADVANCED |
| `notify_app_url` |  | `river/notify.py::_app_channel` | no | ADVANCED |
| `notify_open` | A tap opens | `river/notify.py::open_lines`, `river/notify.py::open_for`, `river/notify.py::status` | no | ADVANCED |
| `ntfy_click` | Phone link when no relay | `river/notify.py::phone_note`, `river/notify.py::send` | no | ADVANCED |
| `ntfy_priority` | ntfy priority | `river/notify.py::send` | no | ADVANCED |
| `ntfy_topic` | ntfy topic | `river/notify.py::_ntfy_channel`, `river/server.py::setup_status` | yes | ADVANCED |
| `ntfy_token` | ntfy token | `river/notify.py::_ntfy_channel` | no | ADVANCED |
| `timezone` | Time zone | `river/core.py::_approval_close_refusal`, `river/core.py::_ops_zone`, `river/core.py::_schedule_zone` | no | ADVANCED |
| `auto_continue` | After a task, take | `river/core.py::_go`, `river/core.py::tree_mode` | yes | NORMAL |
| `result_look` | A finished result is looked at | `river/core.py::look_setup`, `river/core.py::_go_brief`, `river/core.py::project_show` | yes | ADVANCED |
| `goal_approval` |  | `river/core.py::goal_add` | no | ADVANCED |
| `done_proof` |  | `river/core.py::done_proof` | no | ADVANCED |
| `action_type` |  | `river/core.py::_action_types` | yes | ADVANCED |
| `review_at` | A person approves changes before (off, push, release) | `river/review.py::session_diff`, `river/review.py::release_review`, `river/review.py::item_diff` | no | ADVANCED |
| `cost_limit` | Cost limit: an alert at 80% and 100% ($40/day, 20M/week) | `river/cost.py::limits`, `river/cost.py::watch` | no | NORMAL |
| `canvas_max` | Largest canvas | `river/core.py::canvas_put`, `river/routes/browser.py::record_action` | no | ADVANCED |
| `canvas_versions` | Canvas versions kept | `river/core.py::canvas_put` | no | ADVANCED |
| `canvas_network` | What a canvas may load | `river/routes/canvas.py::csp` | no | ADVANCED |
| `browser_allow` | Private addresses the browser may open | `river/browser.py::dev_page`, `river/browser.py::allow` | no | ADVANCED |
| `browser_accounts` | Accounts agents may use | `river/browser.py::accounts`, `river/browser.py::account_change` | yes | ADVANCED |
| `browser_source` | Where the browser comes from | `river/browser.py::start`, `river/browser.py::_attached_now` | yes | ADVANCED |
| `browser_lock` | Command that takes the browser | `river/browser.py::_take_lock`, `river/browser.py::take_from_person`, `river/browser.py::take` | yes | ADVANCED |
| `browser_unlock` | Command that gives the browser back | `river/browser.py::_give_lock` | yes | ADVANCED |
| `browser_mode` | Command that restarts an attached browser | `river/browser.py::attached_mode`, `river/browser.py::_start_attached` | yes | ADVANCED |
| `browser_mcp` | Playwright tools for sessions | `river/browser.py::mcp_config_file` | no | ADVANCED |
| `read_dirs` |  | `river/core.py::read_dir_keys`, `river/core.py::_go_read_dirs` | yes | ADVANCED |
| `browser_pace` | How fast a session moves | `river/browser.py::pace` | no | NORMAL |
| `browser_idle_end` | Close an unused browser after | `river/browser.py::sweep` | no | ADVANCED |
| `browser_take_after` | A session takes the browser from you after | `river/browser.py::why_take` | no | ADVANCED |
| `browser_window_idle` | A browser window goes back into the panel after | `river/browser.py::may_open_window`, `river/browser.py::info`, `river/browser.py::window_sweep` | no | ADVANCED |
| `browser_size` |  | `river/browser.py::default_size` | no | NORMAL |
| `browser_shot_cursor` | Show the pointer in screenshots | `river/browser.py::cdp_session` | no | ADVANCED |
| `web_terminal` | Terminal tab | `river/routes/terminal.py::allowed`, `river/routes/terminal.py::screen` | no | NORMAL |
| `web_terminal_relay` | Terminal tab through the relay | `river/routes/terminal.py::allowed`, `river/routes/terminal.py::screen` | no | ADVANCED |
| `due_warn_before` | Warn before a due date | `river/core.py::_annotate`, `river/core.py::_due_warnings` | no | ADVANCED |
| `launch_agents` | Agents the Start button offers | `river/core.py::launch_entries`, `river/core.py::launch_profiles`, `river/core.py::_check_agent_type` | yes | ADVANCED |
| `native_message` | Send messages into sessions | `river/core.py::native_from_env`, `river/core.py::deliver_native`, `river/core.py::has_native` | no | ADVANCED |
| `setup_done` | Setup guide finished | `river/server.py::setup_status` | yes | ADVANCED |
| `wait_max` | End a waiting session after | `river/core.py::wait`, `river/core.py::capacity` | no | ADVANCED |
| `wait_step` | One wait lasts | `river/core.py::wait` | no | FOLD (internal 9m chunking of maxpm wait (--step flag exists)) |
| `wait_too_long` | Waiting too long after | `river/core.py::manager_findings`, `river/core.py::manage` | no | ADVANCED |
| `unpushed_after` | Unpushed commits are a finding after | `river/core.py::release_waits` | no | ADVANCED |
| `connect_within` | A started session must connect within | `river/core.py::owner_can_deploy`, `river/core.py::_sweep`, `river/core.py::not_connected` | no | ADVANCED |
| `prompt_pattern` | How a waiting prompt looks | `river/server.py::_done_panes`, `river/server.py::_pane_rule`, `river/server.py::watch_prompts` | no | ADVANCED |
| `prompt_wait` | Wait before a prompt counts | `river/server.py::watch_prompts` | no | ADVANCED |
| `prompt_end` |  | `river/server.py::_end_at_prompt` | no | ADVANCED |
| `stuck_after` | Unchanged screen before an alert | `river/server.py::_stuck` | no | ADVANCED |
| `command_hang_after` | Hanging command warning after | `river/server.py::watch_commands` | no | ADVANCED |
| `command_hang_kill` | End a hanging command after | `river/server.py::watch_commands` | no | ADVANCED |
| `activity_silence` | Silence before a session counts as stopped | `river/core.py::silent_holders` | yes | ADVANCED |
| `ops_idle_work` | Idle work probe | `river/core.py::ops_probe` | no | ADVANCED |
| `ops_serve_late` | serve is late after | `river/core.py::_probe_serve` | no | ADVANCED |
| `ops_note_every` | Add a note to an open fault every | `river/core.py::_ops_note` | no | ADVANCED |
| `ops_max_investigations` | Most fault investigations at once | `river/server.py::auto_operational` | no | ADVANCED |
| `ops_repair_max` | Most repairs of one check | `river/core.py::ops_repair_start` | no | ADVANCED |
| `ops_repair_window` | Repair count window | `river/core.py::ops_repair_start` | no | ADVANCED |
| `ops_noise_max` | Longest noise rule | `river/core.py::ops_noise_add` | no | ADVANCED |
| `ops_release_window` | A fault belongs to a release within | `river/core.py::ops_kind` | no | ADVANCED |
| `ops_watch_every` | Watch after a release every | `river/core.py::_ops_watch_due`, `river/core.py::_ops_watch_missed` | no | ADVANCED |
| `ops_watch_cron` | Scheduled checks in the release watch | `river/core.py::_ops_watch` | no | ADVANCED |
| `ops_confirm` | Run a failed check again before a fault | `river/core.py::ops_finish` | no | ADVANCED |
| `net_probe_hosts` |  | `river/core.py::net_probe` | no | ADVANCED |
| `ops_proof_timeout` | Stop a proof command after | `river/core.py::ops_cause` | no | ADVANCED |
| `ops_maps_timeout` | Stop a source map build after | `river/plugins/pages.py::build_maps` | no | ADVANCED |
| `ops_fix_max` | Stop after this many fixes that did not clear a fault | `river/core.py::_ops_fix_limit` | no | ADVANCED |
| `ops_reopen_within` | Reopen a fixed fault within | `river/core.py::_ops_fault_item` | no | ADVANCED |
| `ops_storm_count` | Faults that make a storm | `river/core.py::_ops_fault_item`, `river/operations.py::handle_event` | no | ADVANCED |
| `ops_storm_window` | Storm window | `river/core.py::_ops_fault_item`, `river/operations.py::handle_event` | no | ADVANCED |
| `ops_lease_loop` | Lease loop check | `river/core.py::ops_probe` | no | ADVANCED |
| `ops_release_loop` | Release loop check | `river/core.py::_release_loops` | no | ADVANCED |
| `ops_deadman_url` | Outside heartbeat address | `river/cli.py::ops_setup`, `river/core.py::ops_deadman` | no | ADVANCED |
| `ops_deadman_every` | Send the heartbeat every | `river/cli.py::ops_setup`, `river/core.py::ops_deadman` | no | ADVANCED |
| `ops_outside_repair_max` | Most watchdog restarts | `river/core.py::_ops_outside_one` | no | ADVANCED |
| `ops_outside_repair_window` | Watchdog restart window | `river/core.py::_ops_outside_repairs`, `river/core.py::_ops_outside_one` | no | ADVANCED |
| `push_guard` | Commands held while an alert is unread | `river/core.py::push_guard` | no | ADVANCED |
| `idle_after` | Idle after | `river/core.py::tell_manager_waits`, `river/core.py::owner_can_deploy`, `river/server.py::auto_tidy` | no | ADVANCED |
| `fresh_sessions` | Start a fresh session after an answer | `river/core.py::_want_fresh_comments`, `river/core.py::_want_fresh`, `river/core.py::_go` | no | ADVANCED |
| `serve_runners` | Fast commands (runners) | `river/server.py::serve` | no | FOLD (internal architecture switch, only sensible value is on) |
| `serve_loop_process` | Run the loop in its own process | `river/server.py::serve` | no | FOLD (internal architecture switch, only sensible value is on) |
| `tmux_adopt` | Move sessions into tmux | `river/sessions.py::adopt_check` | no | ADVANCED |
| `revive_sessions` | Resume a session that crashed | `river/server.py::revive_sessions` | no | ADVANCED |
| `revive_within` | Resume only within | `river/server.py::revive_sessions` | no | ADVANCED |
| `idle_end` | End an idle session after | `river/server.py::fresh_sessions` | no | ADVANCED |
| `expert_idle_end` | End a quiet expert after | `river/core.py::expert_view`, `river/core.py::end_quiet_experts` | no | ADVANCED |
| `session_idle_end` | End a quiet session with no item after | `river/core.py::end_quiet_experts`, `river/core.py::_go` | no | ADVANCED |
| `queue` | Use the queue | `river/core.py::_go` | yes | ADVANCED |
| `workspace` | Folder of each session | `river/server.py::_workspace` | yes | ADVANCED |
| `workspace_setup` | Setup command of a new worktree | `river/server.py::_workspace` | no | ADVANCED |
| `session_builds` | Build folders of sessions | `river/scratch.py::build_root` | no | ADVANCED |
| `disk_warn_free` |  | `river/diskguard.py::serve_pass` | no | ADVANCED |
| `disk_sweep_free` |  | `river/diskguard.py::serve_pass` | no | ADVANCED |
| `disk_sweep_cmd` |  | `river/diskguard.py::serve_pass` | yes | ADVANCED |
| `disk_launch_min` |  | `river/core.py::disk_refusal` | no | ADVANCED |
| `tmp_max_size` |  | `river/diskguard.py::check_tmp` | no | ADVANCED |
| `fresh_min` | Fresh session above | `river/core.py::_migrate`, `river/server.py::_resume_for_answer`, `river/server.py::revive_sessions` | no | ADVANCED |
| `cache_ttl` | Prompt cache lasts | `river/sessions.py::cache_state` | no | ADVANCED |
| `model_prices` | Model prices | `river/core.py::model_price` | no | ADVANCED |
| `question_popup_max` | Open 'Waits for you' by itself at most | `river/sessions.py::waiting_for` | no | ADVANCED |
| `question_hold` | The manager sees questions first for | `river/core.py::_hold_question`, `river/core.py::prompt_hold_manager`, `river/core.py::_hold_items` | no | ADVANCED |
| `question_wait_end` | End a session that waits for an answer after | `river/core.py::_wait_end`, `river/core.py::send` | no | ADVANCED |
| `tidy_every` | Close finished panes every | `river/cli.py::render_manage`, `river/core.py::manage`, `river/server.py::auto_tidy` | no | ADVANCED |
| `serve_reload` | Restart serve after a code change | `river/server.py::reload_ready` | no | ADVANCED |
| `serve_reload_min` | Least time between restarts | `river/server.py::reload_ready` | no | ADVANCED |
| `update_check` | Look for a new version | `river/release.py::status`, `river/release.py::check` | no | ADVANCED |
| `agent_update_check` | Look for new agent versions | `river/agentupdate.py::status`, `river/agentupdate.py::serve_pass` | no | ADVANCED |
| `agent_auto_update` | Agents that update by themselves | `river/agentupdate.py::auto_ids` | no | ADVANCED |
| `relay_keep_awake` | Keep this computer awake for the relay | `river/relay.py::want` | no | ADVANCED |
| `busy_max` | Longest busy time without a command | `river/core.py::busy_now`, `river/core.py::keep_busy`, `river/core.py::silent_holders` | no | ADVANCED |
| `shells_every` | Read session commands every | `river/server.py::shells_loop` | no | ADVANCED |
| `sample_every` | Record session states every | `river/core.py::sample_sessions` | no | ADVANCED |
| `sample_keep` | Keep session states for | `river/core.py::sample_sessions` | no | ADVANCED |
| `db_max_size` | Largest queue file | `river/space.py::_db_part`, `river/space.py::serve_pass` | no | ADVANCED |
| `folder_max_size` | Largest queue folder | `river/space.py::_folder_part`, `river/space.py::run`, `river/space.py::serve_pass` | no | ADVANCED |
| `archive_after` | Archive closed items after | `river/space.py::_archive_part` | no | ADVANCED |
| `archive_max_size` | Largest archive | `river/space.py::_db_part` | no | ADVANCED |
| `space_runs_after` |  | `river/space.py::<module>` | no | ADVANCED |
| `space_ops_after` | Keep check runs at least | `river/space.py::<module>` | no | ADVANCED |
| `space_notices_after` | Keep notices at least | `river/space.py::<module>` | no | ADVANCED |
| `space_events_after` | Keep history at least | `river/space.py::<module>` | no | ADVANCED |
| `space_canvas_after` | Keep old canvas versions at least | `river/space.py::<module>` | no | ADVANCED |
| `space_sessions_after` | Keep session records at least | `river/space.py::_candidates`, `river/space.py::<module>` | no | ADVANCED |
| `space_files_after` | Keep uploaded files at least | `river/space.py::_folder_part` | no | ADVANCED |
| `health_person_minutes` | Person minutes per item that needs you | `river/core.py::health` | no | FOLD (internal proxy weight for Health tab) |
| `health_prompt_minutes` |  | `river/core.py::health` | no | FOLD (internal proxy weight for Health tab) |
| `manage_every` | Manager looks every | `river/core.py::manage_watch`, `river/core.py::manage` | no | ADVANCED |
| `manage_wake_count` |  | `river/core.py::manage`, `river/core.py::manage_watch` | no | ADVANCED |
| `manage_quiet_after` |  | `river/core.py::manage_watch` | no | ADVANCED |
| `manage_quiet_every` |  | `river/core.py::manage_watch`, `river/core.py::manage` | no | ADVANCED |
| `manage_settle` | Manager waits for more findings | `river/core.py::manage_watch`, `river/core.py::manage` | no | ADVANCED |
| `manage_wait_high` | Wake the manager for a high message after | `river/core.py::_migrate_levels`, `river/core.py::manage`, `river/core.py::manage_watch` | no | FOLD (same 50m default as manage_every; fold into manage_every) |
| `manage_wait_low` | Wake the manager for a low message after | `river/core.py::manage`, `river/core.py::manage_watch`, `river/core.py::tell_watch_late` | no | FOLD (same 50m default as manage_every; fold into manage_every) |
| `manage_rewake` | Wake the manager again for an open message after | `river/core.py::manage_watch` | no | ADVANCED |
| `manage_unanswered` | An unanswered message is a finding after | `river/core.py::manage_watch`, `river/core.py::tell_unanswered` | no | ADVANCED |
| `manager_autocompact` | Manager context compacts at | `river/core.py::resume_command`, `river/server.py::start_manager`, `river/sessions.py::compacts_at` | no | ADVANCED |
| `stale_after` | An unclaimed item is stale after | `river/core.py::cleanup` | no | ADVANCED |
| `review` | Review before release | `river/core.py::_release_review`, `river/core.py::target_hook`, `river/core.py::_ops_verify` | yes | ADVANCED |
| `review_prompt` | Review prompt | `river/core.py::_release_review` | yes | ADVANCED |
| `review_cmd` | Review command | `river/core.py::_release_review` | yes | ADVANCED |
| `review_timeout` | Stop a review command after | `river/core.py::review_pass`, `river/core.py::must_pass` | no | ADVANCED |
| `hook_timeout` | Stop a hook after | `river/core.py::target_hook`, `river/core.py::hook_results` | no | ADVANCED |
| `deploy_timeout` | Stop a deploy after | `river/core.py::target_hook`, `river/core.py::deploy_hook_lost` | no | ADVANCED |
| `stage_timeout` |  | `river/pipeline/runner.py::_execute` | no | ADVANCED |
| `deps_timeout` |  | `river/pipeline/deps.py::_execute` | no | ADVANCED |
| `list_cleanup_after` | Clean up a person's list after | `river/core.py::cleanup_trigger` | no | ADVANCED |
| `list_cleanup_min` | Clean up a person's list from | `river/core.py::cleanup_trigger` | no | ADVANCED |
| `release_commits` | Command that lists a release's commits | `river/core.py::_release_commits` | yes | ADVANCED |
| `auto_review` | Start reviews by itself | `river/server.py::auto_release` | yes | ADVANCED |
| `max_sessions` | Most sessions at once | `river/server.py::auto_operational`, `river/server.py::auto_release` | no | NORMAL |
| `release_approval` | Release gate | `river/core.py::release_approval` | yes | ADVANCED |
| `model_ladder` | Models, weakest to strongest | `river/core.py::_annotate`, `river/core.py::_model_refusal`, `river/core.py::agent_providers` | no | ADVANCED |
| `effort_levels` | Effort levels | `river/core.py::launch_options`, `river/core.py::_check_effort`, `river/core.py::state` | no | ADVANCED |
| `default_model` | Model for new items | `river/core.py::_effective_models`, `river/core.py::_model_defaults`, `river/core.py::work_model` | yes | NORMAL |
| `default_effort` | Effort for new items | `river/core.py::_effective_models`, `river/core.py::_model_defaults` | no | NORMAL |
| `default_min_model` | Weakest model allowed | `river/core.py::_effective_models`, `river/core.py::_model_defaults` | no | ADVANCED |
| `default_max_model` | Strongest model allowed | `river/core.py::_effective_models`, `river/core.py::_inherit_model`, `river/core.py::_model_defaults` | yes | NORMAL |
| `model_min_version` |  | `river/core.py::model_too_old` | yes | ADVANCED |
| `agent_rules` | Agent type by kind of item | `river/core.py::_model_defaults` | no | ADVANCED |
| `default_agent` | Agent program for new items | `river/core.py::_effective_models`, `river/core.py::_model_defaults`, `river/core.py::role_agent` | yes | NORMAL |
| `usage_probe_every` |  | `river/server.py::probe_usage`, `river/server.py::usage_probes_due` | no | ADVANCED |
| `agent_order` | Agent providers in launch order, with session limits | `river/core.py::agent_providers`, `river/core.py::provider_order_set` | yes | NORMAL |
| `usage_list_hide` | Hidden from the usage list | `river/core.py::usage_list_hidden` | yes | NORMAL |
| `agent_for.worker` | Agent for workers | `river/core.py::_effective_models, river/core.py::role_agent` | no | ADVANCED |
| `agent_for.manager` | Agent for the manager | `river/core.py::_effective_models, river/core.py::role_agent` | no | ADVANCED |
| `agent_for.reviewer` | Agent for reviews | `river/core.py::_effective_models, river/core.py::role_agent` | no | ADVANCED |
| `agent_for.deployer` | Agent for deploys | `river/core.py::_effective_models, river/core.py::role_agent` | no | ADVANCED |
| `agent_for.expert` | Agent for experts | `river/core.py::_effective_models, river/core.py::role_agent` | yes | NORMAL |
| `claude_remote_control` |  | `river/core.py::profile_options (claude-code)` | no | ADVANCED |
| `claude_permission_mode` |  | `river/core.py::profile_options (claude-code)` | no | ADVANCED |
| `claude_model_ids` |  | `river/core.py::profile_options (claude-code)` | no | ADVANCED |
| `claude_args` |  | `river/core.py::profile_options (claude-code)` | yes | ADVANCED |
| `claude_skill_prompt` |  | `river/core.py::profile_options (claude-code)` | no | ADVANCED |
| `claude_prompt` |  | `river/core.py::profile_options (claude-code)` | no | ADVANCED |
| `claude_activity_hooks` |  | `river/core.py::profile_options (claude-code)` | yes | ADVANCED |
| `claude_model` |  | `river/core.py::profile_options (claude-code)` | no | ADVANCED |
| `claude_effort` |  | `river/core.py::profile_options (claude-code)` | no | ADVANCED |
| `codex_sandbox` |  | `river/core.py::profile_options (codex)` | no | ADVANCED |
| `codex_approval` |  | `river/core.py::profile_options (codex)` | no | ADVANCED |
| `codex_model_ids` |  | `river/core.py::profile_options (codex)` | no | ADVANCED |
| `codex_args` |  | `river/core.py::profile_options (codex)` | yes | ADVANCED |
| `codex_prompt` |  | `river/core.py::profile_options (codex)` | no | ADVANCED |
| `codex_model` |  | `river/core.py::profile_options (codex)` | no | ADVANCED |
| `codex_effort` |  | `river/core.py::profile_options (codex)` | no | ADVANCED |
| `agy_mode` |  | `river/core.py::profile_options (antigravity)` | no | ADVANCED |
| `agy_skip_permissions` |  | `river/core.py::profile_options (antigravity)` | no | ADVANCED |
| `agy_sandbox` |  | `river/core.py::profile_options (antigravity)` | no | ADVANCED |
| `agy_args` |  | `river/core.py::profile_options (antigravity)` | no | ADVANCED |
| `agy_activity_hooks` |  | `river/core.py::profile_options (antigravity)` | no | ADVANCED |
| `agy_prompt` |  | `river/core.py::profile_options (antigravity)` | no | ADVANCED |
| `agy_skill_prompt` |  | `river/core.py::profile_options (antigravity)` | no | ADVANCED |
| `agy_model` |  | `river/core.py::profile_options (antigravity)` | yes | ADVANCED |
| `agy_effort` |  | `river/core.py::profile_options (antigravity)` | yes | ADVANCED |
| `pi_tools` |  | `river/core.py::profile_options (pi)` | no | ADVANCED |
| `pi_offline` |  | `river/core.py::profile_options (pi)` | no | ADVANCED |
| `pi_args` |  | `river/core.py::profile_options (pi)` | no | ADVANCED |
| `pi_prompt` |  | `river/core.py::profile_options (pi)` | no | ADVANCED |
| `pi_model` |  | `river/core.py::profile_options (pi)` | no | ADVANCED |
| `pi_effort` |  | `river/core.py::profile_options (pi)` | no | ADVANCED |
| `copilot_permissions` |  | `river/core.py::profile_options (copilot)` | no | ADVANCED |
| `copilot_mode` |  | `river/core.py::profile_options (copilot)` | no | ADVANCED |
| `copilot_args` |  | `river/core.py::profile_options (copilot)` | no | ADVANCED |
| `copilot_prompt` |  | `river/core.py::profile_options (copilot)` | no | ADVANCED |
| `copilot_model` |  | `river/core.py::profile_options (copilot)` | no | ADVANCED |
| `copilot_effort` |  | `river/core.py::profile_options (copilot)` | no | ADVANCED |
| `hermes_provider` |  | `river/core.py::profile_options (hermes)` | no | ADVANCED |
| `hermes_yolo` |  | `river/core.py::profile_options (hermes)` | no | ADVANCED |
| `hermes_checkpoints` |  | `river/core.py::profile_options (hermes)` | no | ADVANCED |
| `hermes_toolsets` |  | `river/core.py::profile_options (hermes)` | no | ADVANCED |
| `hermes_skills` |  | `river/core.py::profile_options (hermes)` | no | ADVANCED |
| `hermes_args` |  | `river/core.py::profile_options (hermes)` | no | ADVANCED |
| `hermes_prompt` |  | `river/core.py::profile_options (hermes)` | no | ADVANCED |
| `hermes_model` |  | `river/core.py::profile_options (hermes)` | no | ADVANCED |
| `hermes_effort` |  | `river/core.py::profile_options (hermes)` | no | ADVANCED |
| `grok_permission_mode` |  | `river/core.py::profile_options (grok)` | no | ADVANCED |
| `grok_sandbox` |  | `river/core.py::profile_options (grok)` | no | ADVANCED |
| `grok_args` |  | `river/core.py::profile_options (grok)` | no | ADVANCED |
| `grok_prompt` |  | `river/core.py::profile_options (grok)` | no | ADVANCED |
| `grok_model` |  | `river/core.py::profile_options (grok)` | no | ADVANCED |
| `grok_effort` |  | `river/core.py::profile_options (grok)` | no | ADVANCED |
| `opencode_standalone` |  | `river/core.py::profile_options (opencode)` | no | ADVANCED |
| `opencode_auto` |  | `river/core.py::profile_options (opencode)` | no | ADVANCED |
| `opencode_args` |  | `river/core.py::profile_options (opencode)` | no | ADVANCED |
| `opencode_prompt` |  | `river/core.py::profile_options (opencode)` | no | ADVANCED |
| `opencode_model` |  | `river/core.py::profile_options (opencode)` | no | ADVANCED |
| `goose_mode` |  | `river/core.py::profile_options (goose)` | no | ADVANCED |
| `goose_max_turns` |  | `river/core.py::profile_options (goose)` | no | ADVANCED |
| `goose_builtins` |  | `river/core.py::profile_options (goose)` | no | ADVANCED |
| `goose_args` |  | `river/core.py::profile_options (goose)` | no | ADVANCED |
| `goose_model` |  | `river/core.py::profile_options (goose)` | no | ADVANCED |
| `goose_effort` |  | `river/core.py::profile_options (goose)` | no | ADVANCED |
| `aider_edit_format` |  | `river/core.py::profile_options (aider)` | no | ADVANCED |
| `aider_auto_commits` |  | `river/core.py::profile_options (aider)` | no | ADVANCED |
| `aider_yes_always` |  | `river/core.py::profile_options (aider)` | no | ADVANCED |
| `aider_cache_prompts` |  | `river/core.py::profile_options (aider)` | no | ADVANCED |
| `aider_gitignore` |  | `river/core.py::profile_options (aider)` | no | ADVANCED |
| `aider_model_warnings` |  | `river/core.py::profile_options (aider)` | no | ADVANCED |
| `aider_analytics` |  | `river/core.py::profile_options (aider)` | no | ADVANCED |
| `aider_weak_model` |  | `river/core.py::profile_options (aider)` | no | ADVANCED |
| `aider_args` |  | `river/core.py::profile_options (aider)` | no | ADVANCED |
| `aider_model` |  | `river/core.py::profile_options (aider)` | no | ADVANCED |
| `aider_effort` |  | `river/core.py::profile_options (aider)` | no | ADVANCED |
| `amp_no_ide` |  | `river/core.py::profile_options (amp)` | no | ADVANCED |
| `amp_quiet` |  | `river/core.py::profile_options (amp)` | no | ADVANCED |
| `amp_fast` |  | `river/core.py::profile_options (amp)` | no | ADVANCED |
| `amp_visibility` |  | `river/core.py::profile_options (amp)` | no | ADVANCED |
| `amp_settings_file` |  | `river/core.py::profile_options (amp)` | no | ADVANCED |
| `amp_args` |  | `river/core.py::profile_options (amp)` | no | ADVANCED |
| `amp_prompt` |  | `river/core.py::profile_options (amp)` | no | ADVANCED |
| `amp_model` |  | `river/core.py::profile_options (amp)` | no | ADVANCED |
| `amp_effort` |  | `river/core.py::profile_options (amp)` | no | ADVANCED |
| `cursor_force` |  | `river/core.py::profile_options (cursor)` | no | ADVANCED |
| `cursor_auto_review` |  | `river/core.py::profile_options (cursor)` | no | ADVANCED |
| `cursor_sandbox` |  | `river/core.py::profile_options (cursor)` | no | ADVANCED |
| `cursor_mode` |  | `river/core.py::profile_options (cursor)` | no | ADVANCED |
| `cursor_trust` |  | `river/core.py::profile_options (cursor)` | no | ADVANCED |
| `cursor_approve_mcps` |  | `river/core.py::profile_options (cursor)` | no | ADVANCED |
| `cursor_args` |  | `river/core.py::profile_options (cursor)` | no | ADVANCED |
| `cursor_prompt` |  | `river/core.py::profile_options (cursor)` | no | ADVANCED |
| `cursor_model` |  | `river/core.py::profile_options (cursor)` | no | ADVANCED |
| `cursor_effort` |  | `river/core.py::profile_options (cursor)` | no | ADVANCED |
