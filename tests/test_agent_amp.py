"""The Amp adapter (river/agents/amp.py, #5179), tested on fixtures recorded from the real CLI (tests/fixtures/amp:
see its README)."""
import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from river import agents, core, server, sessions
from river.agents import amp

FIX = Path(__file__).resolve().parent / "fixtures" / "amp"
TOOLS_THREAD = "T-01a1c9a3-2b79-70d9-81f5-7a64f237d230"
REPLY_THREAD = "T-01a1c9ac-dfba-7600-a580-8862cd749fcc"


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


def export(name="thread_tools.json"):
    return json.loads(fixture(name))


def file_lines(name="thread_tools.json"):
    lines, _ = amp.export_lines(export(name))
    return [json.dumps(x) for x in lines]


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "home").mkdir()
        env = mock.patch.dict(os.environ, {"MAXPM_DB": str(self.root / "t.db"), "HOME": str(self.root / "home"),
                                           "XDG_CONFIG_HOME": str(self.root / "config")})
        env.start()
        self.addCleanup(env.stop)
        for k in ("MAXPM_AGENT", "AMP_SETTINGS_FILE", "AMP_CURRENT_THREAD_ID"):
            os.environ.pop(k, None)
        agents.load(refresh=True)
        self.addCleanup(lambda: agents.load(refresh=True))
        self.a = agents.get("amp")


class Contract(Env):
    def test_it_is_registered_and_passes_the_check_with_each_part_but_commands(self):
        self.assertEqual(agents.check(amp, "amp"), [])
        self.assertEqual((self.a.source, self.a.missing()), ("builtin", []))
        self.assertIs(agents.get("ampcode"), self.a)
        for part in ("describe", "settings", "effort", "launch", "identity", "chat", "state", "detect", "input",
                     "resume", "setup"):
            self.assertTrue(self.a.supports(part), part)
        self.assertFalse(self.a.supports("commands"))  # "/" opens Amp's command palette: no slash commands
        self.assertEqual(self.a.read_commands("/p"), [])
        self.assertEqual((self.a.describe["family"], self.a.describe["models"]), ("multi", []))
        self.assertEqual(self.a.state, {"state": "detect", "session_id": "identity"})
        self.assertEqual(self.a.config_key("no_ide"), "amp_no_ide")

    def test_a_command_inside_amp_is_amps_also_when_amp_was_started_from_claude_code(self):
        self.assertIs(agents.from_env({"AMP_CURRENT_THREAD_ID": TOOLS_THREAD}), self.a)
        self.assertIs(agents.from_env({"AMP_CURRENT_THREAD_ID": TOOLS_THREAD, "CLAUDECODE": "1"}), self.a)
        self.assertIsNone(agents.from_env({"AGENT": "amp"}))
        self.assertEqual(self.a.identity["url"].format(id=TOOLS_THREAD),
                         f"https://ampcode.com/threads/{TOOLS_THREAD}")

    def test_the_mode_is_the_effort_and_max_is_ultra(self):
        self.assertEqual([self.a.effort_name(x) for x in ("low", "medium", "high", "xhigh", "max")],
                         ["low", "medium", "high", "ultra", "ultra"])
        self.assertEqual(self.a.effort_choices(), ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(self.a.effort["label"], "Mode")
        rx = re.compile(amp.EFFORT["read"])
        self.assertEqual(rx.search("amp --mode ultra --no-ide").group(1), "ultra")
        meta = next(x for x in file_lines() if '"type": "meta"' in x)
        self.assertEqual(rx.search(meta).group(1), "medium")


class Launch(Env):
    def test_build_command_puts_each_setting_on_its_flag_and_the_prompt_on_stdin(self):
        argv = self.a.build_command("w1", "/p", "opus", "max",
                                    {"quiet": "on", "fast": "on", "visibility": "private",
                                     "settings_file": "/s.json", "args": "--log-level debug"}, "run maxpm go")
        self.assertEqual(core.shell_command(argv),
                         "printf '%s\\n' 'run maxpm go' | amp --mode ultra --no-ide --no-notifications --fast "
                         "--visibility private --settings-file /s.json --log-level debug")
        self.assertEqual(self.a.build_command(None, None, None, None, {}, None), ["amp", "--no-ide"])
        self.assertEqual(self.a.build_command(None, None, None, None, {"no_ide": "off"}, None), ["amp"])

    def test_the_command_runs_in_a_shell_with_the_prompt_on_stdin(self):
        """The command line as maxpm runs it in a pane, with a fake amp that prints its words and its stdin: a
        prompt with quotes and a new line reaches amp whole."""
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "amp"
        fake.write_text("#!/bin/sh\nprintf 'ARGS:%s\\n' \"$*\"\ncat\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        prompt = "say \"hi\" and it's\nfine"
        line = core.shell_command(self.a.build_command(None, None, None, "high", {}, prompt))
        done = subprocess.run(["sh", "-c", line], capture_output=True, text=True, timeout=30,
                              env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"})
        self.assertEqual(done.stdout, f"ARGS:--mode high --no-ide\n{prompt}\n")

    def test_resume_continues_the_thread_and_keeps_its_mode_when_none_is_given(self):
        self.assertEqual(self.a.resume_command(TOOLS_THREAD, None, None, {}),
                         ["amp", "--no-ide", "threads", "continue", TOOLS_THREAD])
        line = core.shell_command(self.a.resume_command(TOOLS_THREAD, None, "low", {"prompt": "go on"}))
        self.assertEqual(line, f"printf '%s\\n' 'go on' | amp --mode low --no-ide threads continue {TOOLS_THREAD}")
        for bad in (None, "", "abc", "T-1; rm -rf /"):
            self.assertIsNone(self.a.resume_command(bad, None, None, {}))

    def test_a_hand_written_command_is_read_back_only_when_a_profile_builds_it_exactly(self):
        got = self.a.parse_command(shlex.split('echo "run maxpm go" | amp --mode {effort} --no-ide --fast'))
        self.assertEqual(got, {"settings": {"no_ide": "on", "fast": "on"}, "prompt": "run maxpm go"})
        got = self.a.parse_command(shlex.split("printf '%s\\n' 'go' | amp --visibility workspace"))
        self.assertEqual(got, {"settings": {"visibility": "workspace", "no_ide": "off"}, "prompt": "go"})
        for bad in ("amp", 'echo go | amp -x "do it"', "echo go | amp threads continue T-12345678",
                    "echo go | amp --dangerously-allow-all", "echo go | claude", 'echo a b | amp'):
            self.assertIsNone(self.a.parse_command(shlex.split(bad)), bad)
        argv = self.a.build_command(None, None, None, None, {"quiet": "on"}, "go")
        back = self.a.parse_command(shlex.split(core.shell_command(argv)))
        self.assertEqual(back, {"settings": {"no_ide": "on", "quiet": "on"}, "prompt": "go"})


class Screens(Env):
    """DETECT on the recorded panes, read as maxpm serve's pane watcher reads them (the state source of Amp)."""

    def state(self, name):
        rx = server._AnyOf(tuple(self.a.detect["blocked"]))
        lay = server._layout(self.a.detect)
        tail = server._prompt_tail(fixture(name))
        found = server._prompt_found(tail, rx, lay)
        return ("blocked", found) if found else (server._screen_state(tail, self.a.detect), None)

    def test_each_screen_gives_its_state(self):
        for name, want in (("idle", "idle"), ("idle_reply", "idle"), ("connecting", "working"),
                           ("waiting", "working"), ("streaming", "working"), ("approval", "blocked"),
                           ("approval_turn", "blocked"), ("credits", "blocked")):
            self.assertEqual(self.state(f"pane_{name}.txt")[0], want, name)

    def test_a_blocked_screen_gives_its_menu_and_the_tell_instead_option(self):
        _, found = self.state("pane_approval_turn.txt")
        self.assertEqual(found["options"], [("Allow Once", True), ("Reject with feedback", False),
                                            ("Allow All for This Session", False),
                                            ("Allow All for Every Session", False)])
        self.assertEqual(found["hint"], "Enter select")
        self.assertTrue(re.search(self.a.detect["tell_instead"], "Reject with feedback"))
        _, found = self.state("pane_credits.txt")
        self.assertEqual([o for o, _ in found["options"]], ["Add Paid Credits (ampcode.com/pay)", "Retry", "Dismiss"])

    def test_the_pane_watcher_reports_the_state_of_an_amp_session(self):
        """The same look, recorded as maxpm report records a state (server._pane_reports), for an agent whose
        adapter takes its state from the screen."""
        conn = core.connect()
        self.addCleanup(conn.close)
        core.register(conn, "amp-1")
        for name, want in (("pane_streaming.txt", "working"), ("pane_approval_turn.txt", "blocked"),
                           ("pane_idle_reply.txt", "idle")):
            state, found = self.state(name)
            server._pane_reports(conn, {"amp-1": (state, found and found["question"])})
            self.assertEqual(conn.execute("SELECT activity_event FROM agents WHERE name='amp-1'").fetchone()[0],
                             f"report:{want}", name)


class Chat(Env):
    def test_the_export_becomes_a_file_of_finished_messages(self):
        lines, seen = amp.export_lines(export())
        self.assertEqual([x["type"] for x in lines], ["thread"] + ["message"] * 7 + ["meta"])
        self.assertEqual(lines[0]["cwd"], "/Users/me/proj")
        self.assertEqual(lines[-1], {"type": "meta", "at": "2026-11-11T08:08:29.610Z", "title": "Greeting",
                                     "agentMode": "medium", "state": "idle"})
        self.assertFalse(any("signature" in json.dumps(x) for x in lines))
        self.assertEqual(seen["last"], 7)
        self.assertEqual(amp.export_lines(export(), seen), ([], seen))  # nothing new: nothing again

    def test_an_unfinished_message_waits_until_it_is_done(self):
        e = export()
        e["meta"]["lastKnownAgentState"]["state"] = "running"
        e["messages"][-1]["state"] = {"type": "streaming"}
        lines, seen = amp.export_lines(e)
        self.assertEqual(seen["last"], 6)
        self.assertEqual(lines[-1]["state"], "running")
        e = export()
        more, seen = amp.export_lines(e, seen)
        self.assertEqual([(x["type"], x.get("messageId")) for x in more], [("message", 7), ("meta", None)])

    def test_the_recorded_thread_becomes_the_shared_entries(self):
        entries, turn = amp.parse_chat(file_lines())
        self.assertEqual(turn, "idle")
        self.assertEqual([e["role"] for e in entries],
                         ["user", "user", "tool", "result", "assistant", "user", "assistant"])
        tool = entries[2]
        self.assertEqual((tool["name"], tool["id"], tool["detail"]),
                         ("shell_command", "TU-034vd2SK3niHKnl6E2ltcz",
                          "cat screen-cost.txt | head -60; grep -i cost palette-uniq.txt"))
        self.assertEqual(entries[3]["for"], tool["id"])
        self.assertNotIn("suggested-replies", entries[4]["text"])
        self.assertTrue(entries[4]["text"].startswith("Hi!"))
        entries, turn = amp.parse_chat(file_lines("thread_reply.json"))
        self.assertEqual([(e["role"], e["text"]) for e in entries],
                         [("user", "Reply with the single word: ok"), ("assistant", "ok")])

    def test_the_turn_follows_the_thread_state(self):
        line = {"type": "meta", "title": "x", "agentMode": "low", "state": "running"}
        self.assertEqual(amp.parse_chat_line(line), ([], "working"))
        entries, turn = amp.parse_chat_line({**line, "state": "error"})
        self.assertEqual((turn, [e["text"] for e in entries]), ("idle", ["The last turn stopped with an error"]))

    def test_title_cwd_usage_and_settings(self):
        lines = file_lines()
        self.assertEqual(amp.chat_title(lines), "Greeting")
        self.assertEqual(amp.chat_cwd(lines), "/Users/me/proj")
        self.assertEqual(amp.chat_usage(lines), {"input": 10, "output": 726, "cache_read": 107820,
                                                 "cache_write": 54341})
        self.assertEqual(amp.chat_settings(lines), {"effort": "medium", "model": "claude-opus-5-5"})
        self.assertEqual(amp.chat_settings(file_lines("thread_reply.json")), {"effort": "low", "model": "gpt-5.6-terra"})

    def test_refresh_appends_only_what_is_new_and_waits_between_exports(self):
        f = amp.threads_dir() / f"{TOOLS_THREAD}.jsonl"
        e = export()
        e["meta"]["lastKnownAgentState"]["state"] = "running"
        e["messages"][-1]["state"] = {"type": "streaming"}
        self.assertEqual(amp.refresh(TOOLS_THREAD, f, e), 8)
        self.assertEqual(amp.refresh(TOOLS_THREAD, f, export()), 2)
        self.assertEqual(amp.refresh(TOOLS_THREAD, f, export()), 0)
        entries, turn = amp.parse_chat(f.read_text().splitlines())
        self.assertEqual((len(entries), turn), (7, "idle"))
        with mock.patch.object(amp, "_export", side_effect=AssertionError("no export within REFRESH")):
            self.assertEqual(amp.chat_path(TOOLS_THREAD), f)  # the export of a moment ago stands
        os.utime(f.with_name(f.name + ".seen"), (time.time() - 60, time.time() - 60))
        with mock.patch.object(amp, "_export", return_value=None) as got:
            self.assertEqual(amp.chat_path(TOOLS_THREAD), f)
        got.assert_called_once_with(TOOLS_THREAD)

    def test_chat_path_takes_a_thread_id_and_a_root_with_no_export(self):
        root = self.root / "logs"
        root.mkdir()
        (root / f"{REPLY_THREAD}.jsonl").write_text("\n".join(file_lines("thread_reply.json")) + "\n")
        with mock.patch.object(amp, "_export", side_effect=AssertionError("a root is read as it is")):
            self.assertEqual(self.a.chat_path(REPLY_THREAD, root=root), root / f"{REPLY_THREAD}.jsonl")
            self.assertIsNone(self.a.chat_path(TOOLS_THREAD, root=root))
        for bad in ("", "../x", "T-1/../../etc", "abc"):
            self.assertIsNone(amp.chat_path(bad, root=root), bad)

    def test_the_sessions_tab_reads_the_file_line_by_line(self):
        f = self.root / f"{TOOLS_THREAD}.jsonl"
        f.write_text("\n".join(file_lines()) + "\n")
        got = sessions.entries("amp", f)
        self.assertEqual([e["role"] for e in got["entries"]],
                         ["user", "user", "tool", "result", "assistant", "user", "assistant"])
        self.assertEqual(got["turn"], "idle")


class Setup(Env):
    def test_the_mcp_server_goes_into_amp_settings_as_amp_mcp_add_writes_it(self):
        f = self.root / "config" / "amp" / "settings.json"
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps({"amp.permissions": [{"tool": "shell_command", "action": "ask"}]}))
        self.assertEqual(self.a.module.write_mcp_config("http://127.0.0.1:8765/mcp"), [str(f)])
        data = json.loads(f.read_text())
        self.assertEqual(data["amp.mcpServers"], {"maxpm": {"url": "http://127.0.0.1:8765/mcp"}})
        self.assertEqual(data["amp.permissions"], [{"tool": "shell_command", "action": "ask"}])
        self.assertTrue(f.with_name("settings.json.bak").exists())
        self.assertEqual(self.a.module.write_mcp_config("http://127.0.0.1:8765/mcp"), [])

    def test_a_settings_file_with_comments_is_not_rewritten(self):
        f = self.root / "config" / "amp" / "settings.json"
        f.parent.mkdir(parents=True)
        f.write_text('{\n  // mine\n  "amp.updates.mode": "auto"\n}\n')
        with self.assertRaisesRegex(core.RiverError, "amp mcp add maxpm http://h/mcp"):
            self.a.module.write_mcp_config("http://h/mcp")
        self.assertIn("// mine", f.read_text())

    def test_amp_settings_file_moves_the_settings(self):
        with mock.patch.dict(os.environ, {"AMP_SETTINGS_FILE": str(self.root / "s.json")}):
            self.assertEqual(amp.settings_file(), self.root / "s.json")
        self.assertEqual(amp.settings_file(), self.root / "config" / "amp" / "settings.json")


if __name__ == "__main__":
    unittest.main()
