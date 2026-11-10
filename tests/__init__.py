"""The tests never reach the tmux of the person who runs them. Every agent starts in a tmux pane: a test fakes
the pane (a runner, or server.TERMINAL_RUNNER, gets the line that would go into it), the whole of tmux (the fake
server.TMUX_RUNNER), or runs a tmux server of its own (server.TMUX_CMD). With no tmux found (core.tmux_path), a
start refuses; a test of an installed tmux sets core.tmux_path itself.

The tests also run as no session: a session that maxpm serve opened has its name, purpose, and model in
the environment (MAXPM_FOCUS=deploy:<target> makes go look only for a deploy), and a test that runs go or
the MCP server there would read them (#2261). A test that needs one sets it itself. MAXPM_DB stays: a test
names its own database, and the variable never points a test at the real queue by being removed."""
import os

from river import core

for _name in ("MAXPM_AGENT", "MAXPM_AGENT_TYPE", "MAXPM_CHAT", "MAXPM_DESKTOP", "MAXPM_FOCUS", "MAXPM_FORK_OF",
              "MAXPM_MODEL", "MAXPM_QUIET"):
    os.environ.pop(_name, None)

core.tmux_path = lambda: None
core.CODEX_FLAGS = set()  # not the installed Codex: a test of its flags sets them
