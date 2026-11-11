# Hermes fixtures

Recorded on 2026-11-10 from Hermes Agent v0.22.7 (`hermes chat --cli --provider openai-codex -m gpt-6.1-terra
--reasoning low`), run in a tmux pane of 120x40 in an empty scratch folder. tests/test_agent_hermes.py reads them.

- `pane_idle_start.txt`: the screen after the start, at the prompt (the grey text after `❯` is a placeholder).
- `pane_working.txt`: during a turn (a shell command runs).
- `pane_idle.txt`: at the prompt after the turn ended.
- `pane_blocked_clarify.txt`: the clarify tool asks the person a question.
- `pane_blocked_approval.txt`: a command waits for the person's approval (approval mode smart).
- `pane_exit.txt`: the lines that `/quit` prints (the resume command).
- `chat_approval.jsonl`: the session of those screens as `hermes.chat_lines()` reads it from `~/.hermes/state.db`:
  the session row, then one message row per line (two shell commands, the question, the denied command).
- `chat_write_resume.jsonl`: a second session: a shell command, a `write_file`, `/quit`, then
  `hermes chat --resume <id> -q ...` and one more turn in the same session.

Cleaned before the commit: the folder of the run is `/tmp/work/hermes-run`; the banner box above "Welcome to Hermes
Agent!" (it lists the local skills and MCP servers) is cut; `model_config` keeps only `reasoning_config`.
