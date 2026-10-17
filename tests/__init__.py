"""The tests never reach the tmux of the person who runs them. With no tmux found, launch_in auto is a
Terminal tab, which each test fakes (server.TERMINAL_RUNNER); a tmux command needs the fake tmux
(server.TMUX_RUNNER) or a tmux server of its own (server.TMUX_CMD). A test of the tmux default sets
core.tmux_path itself.

The tests also run as no session: a session that maxpm serve opened has its name, purpose, and model in
the environment (MAXPM_FOCUS=deploy:<target> makes go look only for a deploy), and a test that runs go or
the MCP server there would read them (#2261). A test that needs one sets it itself. MAXPM_DB stays: a test
names its own database, and the variable never points a test at the real queue by being removed.

The tests never read the Claude Code settings of the person who runs them either: every folder is trusted
(core.claude_trusted), and a test of the trust check sets it itself."""
import os

from river import core

for _name in ("MAXPM_AGENT", "MAXPM_AGENT_TYPE", "MAXPM_CHAT", "MAXPM_DESKTOP", "MAXPM_FOCUS", "MAXPM_FORK_OF",
              "MAXPM_MODEL", "MAXPM_QUIET"):
    os.environ.pop(_name, None)

core.tmux_path = lambda: None
core.claude_trusted = lambda path: True
