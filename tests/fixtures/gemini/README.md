# Gemini CLI fixtures (#5174, #5216)

For the tests of `river/agents/gemini.py`: Gemini CLI 0.68.0 (`@google/gemini-cli`).

- `pane_*.txt`: screens of a real Gemini CLI 0.68.0 in a tmux pane (110 x 40), taken with `tmux capture-pane -p`
  (#5216): idle (empty, and with a draft), working (`esc to cancel`), after a turn cancelled with Esc, and blocked (a
  shell command that writes, a file edit, folder trust, sign-in). The pane's process (`pane_current_command`) is
  `node`; `ps` on its terminal shows `node /opt/homebrew/bin/gemini`. The folder path in the footer is neutral
  (`~/projects/gemdemo`).
- `pane_blocked_trust_dirs*.txt` (#5818): the trust question of a folder that `--include-directories` adds (the queue
  folder, here `/tmp/claude/demo/.maximizepm`), from a start with `--skip-trust`: alone; under the tool question of a
  first prompt (`--prompt-interactive "make a file please"`), where Enter answered the tool question; and alone again
  after that answer. Made as below, with `-e HOME=<demo folder>`, a `settings.json` in the empty Gemini home that
  selects `gemini-api-key` (no sign-in dialog), and `--skip-trust --include-directories <demo folder>/.maximizepm`.
- `session-2026-11-14T00-15-006c1604.jsonl`: the log that the same session wrote to
  `~/.gemini/tmp/<project>/chats/session-<YYYY-MM-DDTHH-MM>-<first 8 of the id>.jsonl`: a reply, a cancelled turn, a
  read-only shell command (Gemini ran `ls` with no question), a refused edit, an allowed shell command, then
  `gemini --resume <id>` and one more prompt, which Gemini appended to the same file. Paths are neutral
  (`/Users/demo/...`).
- `session-2026-11-14T03-33-88c45dca.jsonl` (#5820): the log of a start made as for `pane_blocked_trust_dirs.txt`,
  then `1. Yes` at the trust question and the prompt `say hello`. After the answer Gemini wrote a user line that
  starts `- **Workspace Directories:**` (the folders and their tree). Paths are neutral (`/Users/demo/...`).
- `session-2026-11-11T07-50-a64cec76.jsonl`, `session-2026-11-11T07-55-5d0c1b2a.jsonl`, `session-cancelled.jsonl`:
  written by hand (#5174) for records the real session did not make: `displayContent`, `$rewindTo`, an error line,
  the summary, an image. Their shapes come from a real log of Gemini CLI 0.64 and from the 0.68.0 source of the chat
  recording service.

## How the real screens were made

No Gemini account was used. `record_server.py` answers the Gemini API on `localhost` with scripted replies (a text,
a slow text, a shell call, an edit call), and Gemini CLI talks to it in API key mode:

```
python3 record_server.py 18733 /tmp/fake.log <demo folder> &
tmux -S /tmp/gem.sock new-session -d -s g -x 110 -y 40 -c <demo folder> \
  -e GEMINI_CLI_HOME=<empty folder> -e GEMINI_API_KEY=local-test \
  -e GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:18733 gemini --model gemini-2.5-flash
```

The first start shows folder trust, then the sign-in dialog: choose "Use Gemini API Key". Then type the prompts `say hello`,
`be slow please` (Esc while it works), `list files here`, `edit the function name` (Esc at the question) and
`make a file please` (Enter at the question).
