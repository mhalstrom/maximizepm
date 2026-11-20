#!/bin/bash
# tx.sh <T> <tmux args...>: tmux of the test serve of queue <T>. TMUX is cleared: when it is set (a shell inside the
# person's tmux), tmux uses that server and ignores TMUX_TMPDIR.
T=$1; shift
exec env -u TMUX -u TMUX_PANE TMUX_TMPDIR="$T" tmux "$@"
