Recorded from Amp CLI 0.0.1794369644 in a tmux pane (120 columns) on macOS, 2026-11-11, for river/agents/amp.py
(#5179). Home and temporary paths are replaced by /Users/me/proj; the host name, the installation and device ids, the
user id, the time zone and the signatures of the thinking blocks are left out.

- thread_tools.json: `amp threads export` of a thread in medium mode (claude-opus-5-5): a prompt that ended at
  Out of Credits, a turn with a shell call that waited at an approval and was rejected, and a second turn.
- thread_reply.json: `amp threads export` of a thread in low mode (gpt-5.6-terra) with a one-word reply; its prompt
  came on stdin (`printf '%s\n' '<prompt>' | amp --mode low --no-ide`).
- pane_idle.txt: a new session at its prompt; pane_idle_reply.txt: at its prompt after a reply.
- pane_connecting.txt, pane_waiting.txt, pane_streaming.txt: a turn that runs (the bottom edge of the input box).
- pane_approval.txt: the approval of a shell call that a plugin command made; pane_approval_turn.txt: the approval
  of a shell call in a turn (the input box under it says Running Tools); pane_credits.txt: the Out of Credits menu.
