#!/bin/bash
# q.sh <T> <maxpm args...>: maxpm of this checkout on the test queue <T>/q.db, in a clean environment (env -i), run in
# <T>/w. A session's own MAXPM_TOKEN, MAXPM_AGENT and Claude Code variables never reach the test queue (#6399).
# TMUX_TMPDIR=<T> gives the test serve a tmux server of its own.
T=$1; shift
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$T/w"
cd "$T/w" && exec env -i HOME="$HOME" PATH="$PATH" TERM=xterm-256color LANG=en_US.UTF-8 USER="$USER" SHELL=/bin/zsh \
  MAXPM_DB="$T/q.db" TMUX_TMPDIR="$T" "$HERE/../../bin/maxpm" "$@"
