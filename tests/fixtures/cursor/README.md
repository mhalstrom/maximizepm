Recorded from cursor-agent 2026.11.11-043ef28 on macOS (#5175), in a tmux pane 120 columns wide, on a Free plan
(model auto). The folder path of the recording is replaced by /Users/person/work/app, and the status line under the
input by `~/work/app · main`.

- `screen-*.txt`: `tmux capture-pane -p` of the pane. trust: the workspace trust question at the first start in a
  folder; working: the spinner at the start of a turn; working-tool: while a shell command ran; working-edit: while
  it wrote a file; idle: after a turn; idle-start: a start with no prompt; blocked-shell: the approval menu of a shell
  command that is not in the allowlist.
- `transcript.jsonl`: ~/.cursor/projects/<workspace key>/agent-transcripts/<chat id>/<chat id>.jsonl of one chat: two
  turns, then a resume (`--resume <chat id>`) with two more. "Your previous response was interrupted..." is a line
  that Cursor wrote itself after a dropped connection. Only the last turn ends in a turn_ended line.
- `transcript-error.jsonl`: a chat whose first turn failed (a model that the plan does not include).
