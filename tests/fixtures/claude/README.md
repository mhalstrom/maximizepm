Claude Code files for the tests of river/agents/claude.py.

pane_menu_model.txt: the screen of the /model menu in a tmux pane (tests/test_slash_commands.py).

queued_mid_turn.jsonl (#5297): the lines that Claude Code 2.1.322 writes when the person sends a message while the
session works. The form of each line is that of real transcripts of 2026-11-11; the texts, ids, times and paths are
new. For each message: a queue-operation line 'enqueue', the tool result that the session waited for, a
queue-operation 'remove' (reason absorbed_mid_turn), and an attachment 'queued_command' with the prompt. The first
prompt is a text; the second is a text and an image block (a 1x1 JPEG). Two more queued_command attachments have
other origins: a message of another session (origin peer) and a background task's notice (origin
task-notification). The real lines have more keys (the rendered system reminder, the git state); the chat reader
reads none of them.
