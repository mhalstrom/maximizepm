OpenCode files for tests/test_agent_opencode.py (#5171), recorded on 2026-11-11 from OpenCode 2.2.7 (Homebrew
opencode-v2) on macOS, in tmux, in a test folder with the free model opencode/big-pickle.

- pane-*.txt: `tmux capture-pane -p` of the TUI (trailing spaces cut): a new session (idle), the end of a turn
  (idle-after-turn), a running turn (working), a running /compact (compacting), and the permission screen of a read
  of .env (blocked).
- commands.txt: the names of the TUI's "/" list, scrolled to the end (46 built-in commands).
- session.jsonl and failed.jsonl: river.agents.opencode.export_lines() of two sessions of OpenCode's database
  (~/.local/share/opencode/opencode.db): the session_v2 header, then the session_message rows. session.jsonl has a
  read, a read outside the folder, a rejected read of .env (interrupted), a write and an edit, and a /compact.
  failed.jsonl has a turn that failed ('Free usage limit reached').
- events.jsonl: the events that a probe TUI plugin got from api.data.listen in one run (a read of .env, its
  permission screen, Reject), each with the route (api.ui.router.current()) and the session status at that event.
- reports.txt: the maxpm report and maxpm release commands that the state plugin (install_hooks) ran in the same run,
  with the real maxpm and a test queue (its folder is written /QUEUE).
