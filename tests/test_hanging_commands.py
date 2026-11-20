"""Tests for #6497: hanging child command detection, auto-termination, manager watch, kill-command, and briefing."""

import argparse
import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from river import cli, core, rights, server


class FakeClock:
    def __init__(self, start=None):
        self.t = start or datetime(2026, 11, 20, 22, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def _ps_lines(rows):
    """Build ps output lines: rows of (pid, ppid, etime, cmd)."""
    return "\n".join(f"{pid} {ppid} {etime} {cmd}" for pid, ppid, etime, cmd in rows)


class HangingCommandsUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")
        self.c = core.connect(self.db)
        self.addCleanup(self.c.close)
        core.register(self.c, "mark", human=True)
        core.register(self.c, "mgr")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='manager' WHERE name='mgr'")
        core.project_add(self.c, "proj", actor="mark")
        self.clock = FakeClock()
        p_now = mock.patch.object(core, "now", self.clock)
        p_now.start()
        self.addCleanup(p_now.stop)
        server.CMD_HANG.clear()
        self.addCleanup(server.CMD_HANG.clear)

    def test_settings_defaults_and_validation(self):
        self.assertEqual(core.DEFAULT_SETTINGS["command_hang_after"], "20m")
        self.assertEqual(core.DEFAULT_SETTINGS["command_hang_kill"], "5m")
        core.config_set(self.c, "command_hang_after", "15m", actor="mark")
        core.config_set(self.c, "command_hang_kill", "3m", actor="mark")
        self.assertEqual(core.setting(self.c, "command_hang_after"), "15m")
        self.assertEqual(core.setting(self.c, "command_hang_kill"), "3m")
        with self.assertRaises(core.RiverError):
            core.config_set(self.c, "command_hang_after", "never", actor="mark")
        with self.assertRaises(core.RiverError):
            core.config_set(self.c, "command_hang_kill", "never", actor="mark")

    def test_process_tree_detection_claude_codex_and_agy(self):
        # Claude Code: claude -> mcp (within grace), caffeinate, report --wake, maxpm wait, and zsh -c eval 'npm test'
        # Codex: codex -> sandbox-exec -> zsh -lc 'pytest tests/' -> python3
        # agy: antigravity -> language_server (within grace) -> zsh -c 'npm --prefix push-worker run test:integration' -> npm -> node
        ps = _ps_lines([
            # Claude Code
            (1000, 1, "30:00", "claude --model opus"),
            (1001, 1000, "29:58", "node /opt/mcp/server.js"),
            (1002, 1000, "02:00", "caffeinate -i -t 300"),
            (1003, 1000, "25:00", "/usr/bin/python3 -m river.cli report --wake claude-1"),
            (1004, 1000, "20:00", "/bin/zsh -c maxpm --as claude-1 wait"),
            (1005, 1000, "22:00", "/bin/zsh -c source /tmp/snap && eval 'npm test' < /dev/null"),
            (1006, 1005, "22:00", "npm test"),
            (1007, 1006, "21:59", "node --test"),
            # Codex
            (2000, 1, "30:00", "codex"),
            (2001, 2000, "21:00", "sandbox-exec -f /tmp/sb.sb /bin/zsh -lc pytest tests/"),
            (2002, 2001, "21:00", "/bin/zsh -lc pytest tests/"),
            (2003, 2002, "20:59", "python3 -m pytest tests/"),
            # agy
            (3000, 1, "45:00", "antigravity --conversation abc"),
            (3001, 3000, "44:59", "/usr/local/bin/language_server --stdio"),
            (3002, 3001, "40:00", "/bin/zsh -c npm --prefix push-worker run test:integration"),
            (3003, 3002, "40:00", "npm --prefix push-worker run test:integration"),
            (3004, 3003, "39:59", "node --test test/wrangler.test.js --test-timeout=0"),
        ])
        got = core.session_child_commands(ps, {"claude-1": 1000, "codex-1": 2000, "agy-1": 3000}, self.clock())
        self.assertEqual([(c["pid"], c["label"]) for c in got["claude-1"]], [(1005, "npm test")])
        self.assertEqual([(c["pid"], c["label"]) for c in got["codex-1"]], [(2002, "pytest tests/")])
        self.assertEqual([(c["pid"], c["label"]) for c in got["agy-1"]],
                         [(3002, "npm --prefix push-worker run test:integration")])

    def test_watch_commands_warns_reports_to_manager_and_kills(self):
        core.register(self.c, "agy-9a49")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid=3000, host=? WHERE name='agy-9a49'", (core.this_host(),))
        item = core.item_add(self.c, "proj", "run integration tests", actor="mark")["id"]
        core.claim(self.c, item, "agy-9a49")

        pane_screen = {"text": "running npm test...", "ended": False, "target": "maxpm:0.1", "tty": "/dev/ttys001"}
        ps = _ps_lines([
            (3000, 1, "45:00", "antigravity"),
            (3001, 3000, "44:59", "language_server"),
            (3002, 3001, "01:00", "/bin/zsh -c npm --prefix push-worker run test:integration"),
            (3003, 3002, "01:00", "node --test test/wrangler.test.js --test-timeout=0"),
        ])
        killed_calls = []

        def fake_kill(conn, agent, pid=None, **kw):
            killed_calls.append((agent, pid, kw.get("by_serve")))
            core.set_hanging_commands(conn, {})
            return {"agent": agent, "root_pid": 3000, "pid": pid, "command": "npm --prefix push-worker run test:integration",
                    "killed_pids": [3003, 3002], "force_killed": []}

        with mock.patch.object(core, "SHELL_RUNNER", lambda: ps), \
             mock.patch.object(server, "TMUX_RUNNER", lambda *a, **kw: ""), \
             mock.patch.object(server, "agent_terminals", lambda c: {"agy-9a49": "%1"}), \
             mock.patch.object(server, "_pane_screen", lambda a, p: dict(pane_screen)), \
             mock.patch.object(core, "kill_session_command", side_effect=fake_kill):
            # Pass 1 at T=0: starts tracking, no warning yet
            r1 = server.watch_commands(self.c)
            self.assertEqual((r1["warned"], r1["killed"], r1["hanging"]), ([], [], {}))

            # Advance 15m: still under 20m threshold
            self.clock.advance(minutes=15)
            r2 = server.watch_commands(self.c)
            self.assertEqual((r2["warned"], r2["killed"]), ([], []))

            # New pane output resets the 20m timer!
            pane_screen["text"] = "running npm test...\n1 test passed"
            self.clock.advance(minutes=10)
            r3 = server.watch_commands(self.c)
            self.assertEqual((r3["warned"], r3["killed"]), ([], []))

            # Now 20m with NO new pane output -> warning note + manager finding
            self.clock.advance(minutes=20)
            r4 = server.watch_commands(self.c)
            self.assertEqual(r4["warned"], ["agy-9a49"])
            self.assertEqual(r4["killed"], [])
            self.assertIn("agy-9a49", r4["hanging"])

            # Session received a note with the command and its time
            inbox_ag = core.inbox(self.c, "agy-9a49", mark_read=False)
            self.assertTrue(any("npm --prefix push-worker run test:integration" in m["body"] and "20m" in m["body"]
                                for m in inbox_ag), inbox_ag)

            # Manager findings show COMMAND HANGS <session> <command> <minutes>
            f = core.manager_findings(self.c)
            self.assertEqual(len(f["hanging_commands"]), 1)
            self.assertIn("hanging:agy-9a49:3002", core._finding_keys(f))
            lines = "\n".join(cli._findings_lines(f, "maxpm"))
            self.assertIn("COMMAND HANGS agy-9a49 npm --prefix push-worker run test:integration 20m", lines)
            self.assertIn("maxpm session kill-command agy-9a49 --pid 3002", lines)

            # Advance 4m (less than 5m kill wait): still warned, not killed yet
            self.clock.advance(minutes=4)
            r5 = server.watch_commands(self.c)
            self.assertEqual((r5["warned"], r5["killed"]), ([], []))

            # Advance 1m more (5m after warning): kills the command, notifies session and manager
            self.clock.advance(minutes=1)
            r6 = server.watch_commands(self.c)
            self.assertEqual(r6["killed"], ["agy-9a49"])
            self.assertEqual(killed_calls, [("agy-9a49", 3002, True)])
            self.assertEqual(core.hanging_commands(self.c), {})

            inbox_ag2 = core.inbox(self.c, "agy-9a49", mark_read=False)
            self.assertTrue(any("ended your hanging command" in m["body"] for m in inbox_ag2), inbox_ag2)
            inbox_mgr = core.inbox(self.c, "mgr", mark_read=False)
            self.assertTrue(any("ended a hanging command in agy-9a49" in m["body"] for m in inbox_mgr), inbox_mgr)

    def test_briefing_tells_agents_to_give_long_tests_a_timeout(self):
        core.register(self.c, "w1")
        core.item_add(self.c, "proj", "fix widget", actor="mark")
        b = core.go(self.c, self.tmp.name, "w1", project="proj")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(b)
        self.assertIn("Give long tests and commands a timeout", out.getvalue())
        # Also present in short form on repeat briefing
        b["has_history"] = True
        b["new_name"] = False
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            cli.render_go(b)
        self.assertIn("timeout", out2.getvalue())

    def test_cli_session_kill_command_routes_through_serve_and_renders(self):
        self.assertIs(rights.OPS["session_kill_command"], rights.LEAD)
        called = []

        def fake_ask(conn, op, args, actor=None, timeout=5):
            called.append((op, args, actor))
            return {"agent": "agy-9a49", "root_pid": 3000, "pid": 3002, "command": "npm test",
                    "killed_pids": [3003, 3002], "force_killed": [3003]}

        out = io.StringIO()
        with mock.patch.object(cli, "ask_server", side_effect=fake_ask), contextlib.redirect_stdout(out):
            parser = cli.build_parser()
            args = parser.parse_args(["--as", "mgr", "session", "kill-command", "agy-9a49", "--pid", "3002",
                                      "--reason", "hung 30m"])
            res = cli.dispatch(self.c, args, "mgr")
            cli.render(args, res)
        self.assertEqual(called, [("session_kill_command",
                                   {"agent": "agy-9a49", "pid": 3002, "reason": "hung 30m"}, "mgr")])
        self.assertIn("agy-9a49: ended child command PID 3002 (npm test); signaled 2 processes, SIGKILLed 1",
                      out.getvalue())


class FakeAgentRealProcessTests(unittest.TestCase):
    """Real subprocess tests: a fake agent with a sleeping child (and an unrelated sibling process)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")
        self.c = core.connect(self.db)
        self.addCleanup(self.c.close)
        core.register(self.c, "mark", human=True)
        core.register(self.c, "mgr")
        core.register(self.c, "worker")
        core.register(self.c, "fake-ag")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET role='manager' WHERE name='mgr'")
        self.procs = []
        self.addCleanup(self._cleanup_procs)
        server.CMD_HANG.clear()
        self.addCleanup(server.CMD_HANG.clear)

    def _cleanup_procs(self):
        for p in self.procs:
            if p.poll() is None:
                try:
                    p.kill()
                except OSError:
                    pass
                try:
                    p.wait(timeout=3)
                except subprocess.SubprocessError:
                    pass
            if p.stdout:
                try:
                    p.stdout.close()
                except OSError:
                    pass

    def _spawn_fake_agent_with_child(self, ignore_term=False):
        """Spawn a fake agent python process that launches a sleeping child, waits until the child is ready
        (signal handler installed), prints its child PID, waits for the child to exit, prints DONE:<rc>, and stays alive."""
        child_code = (
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('READY', flush=True); time.sleep(60)"
            if ignore_term else "import time; print('READY', flush=True); time.sleep(60)"
        )
        agent_code = (
            "import subprocess, sys, time; "
            f"c = subprocess.Popen([sys.executable, '-c', {child_code!r}], stdout=subprocess.PIPE, text=True); "
            "c.stdout.readline(); "
            "print(c.pid, flush=True); "
            "rc = c.wait(); "
            "print(f'DONE:{rc}', flush=True); "
            "time.sleep(60)"
        )
        agent = subprocess.Popen([sys.executable, "-c", agent_code], stdout=subprocess.PIPE, text=True)
        self.procs.append(agent)
        line = agent.stdout.readline().strip()
        self.assertTrue(line.isdigit(), f"fake agent did not report child pid: {line!r}")
        child_pid = int(line)
        return agent, child_pid

    def test_kill_session_command_ends_sleeping_child_keeps_agent_and_refuses_other_pids(self):
        agent, child_pid = self._spawn_fake_agent_with_child(ignore_term=False)
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.procs.append(unrelated)
        self.addCleanup(lambda: core.pid_alive(child_pid) and os.kill(child_pid, signal.SIGKILL))

        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid=?, host=? WHERE name='fake-ag'", (agent.pid, core.this_host()))

        ps = _ps_lines([
            (agent.pid, os.getpid(), "30:00", "python3 fake_agent.py"),
            (child_pid, agent.pid, "25:00", "/bin/sh -c sleep 60"),
            (unrelated.pid, os.getpid(), "10:00", "python3 unrelated.py"),
        ])

        with mock.patch.object(core, "SHELL_RUNNER", lambda: ps):
            # A regular worker is refused
            with self.assertRaisesRegex(core.RiverError, "a person or a manager"):
                core.kill_session_command(self.c, "fake-ag", pid=child_pid, actor="worker")

            # Refuses the agent's own PID
            with self.assertRaisesRegex(core.RiverError, "own agent process"):
                core.kill_session_command(self.c, "fake-ag", pid=agent.pid, actor="mgr")

            # Refuses an unrelated PID not under the agent
            with self.assertRaisesRegex(core.RiverError, "is not under fake-ag"):
                core.kill_session_command(self.c, "fake-ag", pid=unrelated.pid, actor="mgr")
            self.assertTrue(core.pid_alive(unrelated.pid))

            # Manager kills the child command (no --pid needed: picks the running child)
            res = core.kill_session_command(self.c, "fake-ag", actor="mgr", reason="hung test", grace=2.0)
            self.assertEqual(res["pid"], child_pid)
            self.assertIn(child_pid, res["killed_pids"])

        # Sleeping child was terminated with SIGTERM and reaped by fake agent; fake agent & unrelated stay alive!
        done_line = agent.stdout.readline().strip()
        self.assertEqual(done_line, f"DONE:{-signal.SIGTERM}")
        self.assertFalse(core.pid_alive(child_pid))
        self.assertIsNone(agent.poll(), "fake agent itself must stay alive")
        self.assertIsNone(unrelated.poll(), "unrelated process must stay alive")

    def test_watch_commands_escalates_term_to_kill_on_stubborn_child(self):
        agent, child_pid = self._spawn_fake_agent_with_child(ignore_term=True)
        self.addCleanup(lambda: core.pid_alive(child_pid) and os.kill(child_pid, signal.SIGKILL))

        with core.tx(self.c):
            self.c.execute("UPDATE agents SET pid=?, host=? WHERE name='fake-ag'", (agent.pid, core.this_host()))

        ps = _ps_lines([
            (agent.pid, os.getpid(), "30:00", "python3 fake_agent.py"),
            (child_pid, agent.pid, "25:00", "/bin/sh -c stubborn_test"),
        ])
        clock = FakeClock()
        with mock.patch.object(core, "now", clock), \
             mock.patch.object(core, "SHELL_RUNNER", lambda: ps), \
             mock.patch.object(server, "agent_terminals", lambda c: {}):
            server.watch_commands(self.c, grace=0.4)
            clock.advance(minutes=20)
            r_warn = server.watch_commands(self.c, grace=0.4)
            self.assertEqual(r_warn["warned"], ["fake-ag"])
            clock.advance(minutes=5)
            r_kill = server.watch_commands(self.c, grace=0.4)
            self.assertEqual(r_kill["killed"], ["fake-ag"])
            done_line = agent.stdout.readline().strip()
            self.assertEqual(done_line, f"DONE:{-signal.SIGKILL}")
            self.assertFalse(core.pid_alive(child_pid))
            self.assertIsNone(agent.poll(), "fake agent itself must stay alive after child is SIGKILLed")


if __name__ == "__main__":
    unittest.main()
