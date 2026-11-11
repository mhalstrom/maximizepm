"""The Aider adapter, river/agents/aider.py (#5177), against the screens and the chat log in tests/fixtures/aider.

The fixtures come from Aider 0.86.2 in a tmux pane (120x40), with a local stand-in for an OpenAI-compatible model
(no real model and no real key): the start questions, the idle prompt in code and diff mode, the wait for the first
token, a reply while it streams, each question that waited for a person, and the chat log of a session and its
resume (--restore-chat-history). The paths in them are made general (/Users/demo)."""
import contextlib
import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import agents, cli, core, server, sessions
from river.agents import aider

FIX = Path(__file__).resolve().parent / "fixtures" / "aider"


def screen(name):
    return (FIX / f"screen-{name}.txt").read_text()


def chat_lines():
    return (FIX / "chat.history.md").read_text().splitlines()


def watch(text):
    """What maxpm serve's pane watcher (server.watch_prompts, #5183) reads from a screen with Aider's DETECT:
    (state, question). state: blocked, working, idle, or None (the screen says neither: the last state stays)."""
    tail = server._prompt_tail(server._plain(text))
    parts = server._prompt_found(tail, server._AnyOf(aider.DETECT["blocked"]), server._layout(aider.DETECT))
    if parts:
        return "blocked", parts["question"]
    return server._screen_state(tail, aider.DETECT), None


class Env(unittest.TestCase):
    """A temporary queue (MAXPM_DB) and home, so no test reads or writes the person's own files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "home").mkdir()
        env = mock.patch.dict(os.environ, {"MAXPM_DB": str(self.root / "t.db"), "HOME": str(self.root / "home")})
        env.start()
        self.addCleanup(env.stop)
        agents.load(refresh=True)
        self.addCleanup(lambda: agents.load(refresh=True))
        self.a = agents.get("aider")


class Describe(Env):
    def test_it_is_a_built_in_adapter_with_every_part_but_hooks_and_mcp(self):
        self.assertEqual(agents.check(aider, "aider"), [])
        self.assertEqual((self.a.source, self.a.missing()), ("builtin", []))
        self.assertIs(agents.get("aider-chat"), self.a)
        for part in ("describe", "settings", "effort", "launch", "identity", "chat", "state", "detect", "input",
                     "resume", "commands", "setup"):
            self.assertTrue(self.a.supports(part), part)
        self.assertEqual(self.a.state["state"], "detect")  # Aider has no hook or plugin: the screen is the source
        self.assertIsNone(getattr(aider, "install_hooks", None))
        self.assertEqual(self.a.describe["family"], "multi")
        self.assertEqual(self.a.config_key("edit_format"), "aider_edit_format")

    def test_a_command_that_aider_runs_is_found_by_the_variable_build_command_sets(self):
        self.assertIs(agents.from_env({"MAXPM_AIDER_SESSION": "w1"}), self.a)
        self.assertIs(agents.from_env({"MAXPM_AIDER_SESSION": "w1", "CLAUDECODE": "1"}), self.a)
        self.assertIsNone(agents.from_env({"MAXPM_AIDER_SESSION": ""}))

    def test_effort_maps_the_top_levels_to_high(self):
        self.assertEqual(self.a.effort_name("max"), "high")
        self.assertEqual(self.a.effort_name("xhigh"), "high")
        self.assertEqual(self.a.effort_name("low"), "low")
        self.assertTrue(set(aider.EFFORT["map"].values()) <= set(aider.EFFORT["levels"]))

    def test_the_effort_reads_back_from_a_command_line_and_from_the_model_line(self):
        rx = re.compile(aider.EFFORT["read"])
        self.assertEqual(rx.search("aider --model sonnet --reasoning-effort high --no-auto-commits").group(1), "high")
        self.assertEqual(rx.search("Model: openai/fake-model with diff edit format, reasoning high").group(1), "high")
        self.assertIsNone(rx.search("Model: openai/fake-model with whole edit format"))

    def test_each_setting_and_its_live_command_have_the_shape_of_the_contract(self):
        keys = [s["key"] for s in aider.SETTINGS]
        self.assertEqual(len(keys), len(set(keys)))
        live = {s["key"]: s["live"] for s in aider.SETTINGS if s["live"]}
        self.assertEqual(live, {"edit_format": "/chat-mode {value}", "weak_model": "/weak-model {value}"})
        names = {c["name"] for c in aider.COMMANDS}
        for cmd in list(live.values()) + [aider.EFFORT["live"]]:
            self.assertIn(cmd.split()[0][1:], names)  # each live command is one of Aider's commands


class Launch(Env):
    def test_a_start_has_the_sessions_own_log_and_the_variable_and_no_prompt(self):
        argv = self.a.build_command("w1", None, "anthropic/claude-sonnet-4-5", "max",
                                    {"edit_format": "diff", "auto_commits": "off", "yes_always": "on"}, "run maxpm go")
        log = str(self.root / "aider" / "w1.chat.history.md")
        self.assertEqual(argv, ["aider", "--model", "anthropic/claude-sonnet-4-5", "--reasoning-effort", "high",
                                "--edit-format", "diff", "--no-auto-commits", "--yes-always",
                                "--chat-history-file", log, "--no-show-release-notes",
                                "--set-env", "MAXPM_AIDER_SESSION=w1"])
        self.assertNotIn("run maxpm go", argv)  # FIRST_PROMPT: the start path pastes it
        self.assertEqual(aider.FIRST_PROMPT, "paste")

    def test_the_defaults_add_no_setting_flag_and_args_go_in_as_written(self):
        argv = self.a.build_command("w1", None, None, None, {"args": "--map-tokens 0  --vim"}, None)
        self.assertEqual(argv[:1] + argv[-1:], ["aider", "--map-tokens 0  --vim"])
        self.assertIsInstance(argv[-1], agents.Raw)
        self.assertTrue(core.shell_command(argv).endswith(" --map-tokens 0  --vim"))
        for flag in ("--model", "--reasoning-effort", "--edit-format", "--no-auto-commits", "--yes-always",
                     "--cache-prompts", "--weak-model", "--restore-chat-history", "--read"):
            self.assertNotIn(flag, argv)

    def test_agents_md_and_the_skill_go_in_as_read_only_files(self):
        folder = self.root / "proj"
        folder.mkdir()
        (folder / "AGENTS.md").write_text("# rules\n")
        with mock.patch.object(core, "skill_prompt_args", lambda args: "--append-system-prompt-file /x/skill.md"):
            argv = self.a.build_command("w1", str(folder), None, None, {"system_prompt_file": True}, None)
        self.assertEqual(argv[-4:], ["--read", "/x/skill.md", "--read", "AGENTS.md"])

    def test_no_name_leaves_aiders_own_log(self):
        argv = self.a.build_command(None, None, "sonnet", None, {}, None)
        self.assertEqual(argv, ["aider", "--model", "sonnet", "--no-show-release-notes"])

    def test_a_hand_written_command_that_a_profile_builds_reads_back(self):
        got = self.a.parse_command(["aider", "--model", "{model}", "--reasoning-effort", "{effort}",
                                    "--chat-mode", "architect", "--no-auto-commits", "--cache-prompts"])
        self.assertEqual(got, {"settings": {"edit_format": "architect", "auto_commits": "off", "cache_prompts": "on"},
                               "prompt": None})
        self.assertEqual(self.a.parse_command(["/usr/local/bin/aider"]), {"settings": {}, "prompt": None})
        for other in (["aider", "--model", "gpt-4o"], ["aider", "--message", "hi"], ["aider", "--api-key", "x=y"],
                      ["claude", "--model", "{model}"], []):
            self.assertIsNone(self.a.parse_command(other), other)

    def test_a_resume_appends_to_the_log_of_the_session_it_resumes(self):
        self.assertIsNone(self.a.resume_command("w1"))  # no log yet: nothing to restore
        log = self.root / "aider" / "w1.chat.history.md"
        log.parent.mkdir()
        log.write_text((FIX / "chat.history.md").read_text())
        argv = self.a.resume_command("w1", "sonnet", "medium", {"prompt": "go on", "fork": "x"}, name="w2")
        self.assertEqual(argv, ["aider", "--model", "sonnet", "--reasoning-effort", "medium",
                                "--chat-history-file", str(log), "--restore-chat-history", "--no-show-release-notes",
                                "--set-env", "MAXPM_AIDER_SESSION=w2"])
        self.assertEqual(self.a.resume_command(str(log))[:4], ["aider", "--chat-history-file", str(log),
                                                                "--restore-chat-history"])

    def test_a_maxpm_command_inside_aider_records_the_name_and_the_sessions_tab_finds_the_log(self):
        self.assertEqual(aider.IDENTITY["source"], "env")
        self.assertEqual(core.session_from_env({"MAXPM_AIDER_SESSION": "maxpm-181d"}), "maxpm-181d")
        for bad in ("../x", "*", "a/b", ""):
            self.assertIsNone(core.session_from_env({"MAXPM_AIDER_SESSION": bad}), bad)
        log = aider.chat_file("w")
        log.parent.mkdir(parents=True)
        log.write_text((FIX / "chat.history.md").read_text())
        c = core.connect()
        self.addCleanup(c.close)
        core.register(c, "w")
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CODEX", "MAXPM_AGENT"))}
        with mock.patch.dict(os.environ, {**clean, "MAXPM_AIDER_SESSION": "w"}, clear=True), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.run(["--as", "w", "who"])  # a command that Aider runs for the person (/run, a proposed command)
        self.assertEqual([r[0] for r in c.execute("SELECT session_id FROM agent_sessions WHERE agent='w'")], ["w"])
        self.assertEqual(sessions.transcript(c, "w"), ("aider", log))
        self.assertEqual(aider.chat_file("a/b c").name, "a_b_c.chat.history.md")


class Detect(Env):
    """Each recorded screen, read as the pane watcher reads it."""

    def test_the_empty_input_line_is_idle(self):
        for name in ("idle", "idle-after-edit", "idle-diff-mode", "idle-resumed"):
            self.assertEqual(watch(screen(name)), ("idle", None), name)

    def test_the_wait_for_the_model_is_working_and_a_streaming_reply_keeps_it(self):
        self.assertEqual(watch(screen("working-waiting")), ("working", None))
        # The prompt that was sent ('> Add a script ...') is no idle line, and a reply shows no marker.
        self.assertEqual(watch(screen("working-streaming")), (None, None))
        self.assertEqual(watch(screen("working-streaming").rstrip() + "\nif a > b:\n"), (None, None))

    def test_each_question_of_aider_is_blocked_with_its_question(self):
        questions = {"gitignore": "Add .aider* to .gitignore (recommended)?",
                     "model-warning": "Open documentation url for more info?",
                     "release-notes": "Would you like to see what's new in this version?",
                     "create-file": "Create new file?", "lint": "Attempt to fix lint errors?",
                     "run-output": "Add 0.0k tokens of command output to the chat?", "shell": "Run shell command?"}
        for name, q in questions.items():
            self.assertEqual(watch(screen(f"blocked-{name}")), ("blocked", q), name)

    def test_a_question_whose_colon_went_to_the_next_line_in_a_narrow_pane(self):
        # Recorded at 57 columns (#5265): '... (Y)es/(N)o [Yes]' and ':' alone on the line under it.
        narrow = screen("blocked-gitignore-narrow")
        self.assertIn("[Yes]\n:\n", narrow)
        self.assertEqual(watch(narrow), ("blocked", "Add .aider* to .gitignore (recommended)?"))
        # The person answered: ': y' joined to the question matches no more, so it waits no more.
        self.assertEqual(watch(narrow.replace("[Yes]\n:\n", "[Yes]\n: y\n")), (None, None))
        # Text under the question that is not its rest: no prompt.
        self.assertEqual(watch(narrow.replace("[Yes]\n:\n", "[Yes]\nRunning git add .gitignore\n")), (None, None))

    def test_typed_text_and_an_answered_question_say_neither(self):
        # What the person typed and has not sent yet: the state stays idle.
        self.assertEqual(watch(screen("idle-diff-mode").rstrip() + " what next"), (None, None))
        # An answered question with its command still at work: no spinner, so the state stays as it was.
        self.assertEqual(watch(screen("blocked-shell").rstrip() + " y\nRunning python3 hello.py\n"), (None, None))

    def test_the_process_and_the_menu_rules(self):
        self.assertEqual(aider.DETECT["process"], "aider")
        self.assertIsNone(aider.DETECT["tell_instead"])
        self.assertIsNone(aider.DETECT["image_paste"])


class Chat(Env):
    def test_the_log_becomes_the_shared_entries(self):
        entries, turn = aider.parse_chat(chat_lines())
        roles = [e["role"] for e in entries]
        self.assertTrue(set(roles) <= set(agents.ENTRY_ROLES))
        users = [e["text"] for e in entries if e["role"] == "user"]
        self.assertEqual(users[:3], ["Add a script that prints a greeting", "/ask How do I run it?",
                                     "How do I run it?"])
        self.assertEqual(users[-2:], ["What did we do so far?", "/exit"])
        first = next(e for e in entries if e["role"] == "assistant")
        self.assertEqual(first["text"], "I will add a small script.\n\nhello.py\n```python\nprint('hello from aider')\n```")
        self.assertIsNone(turn)  # the log ends in /exit

    def test_edits_and_shell_commands_are_tool_entries(self):
        entries, _ = aider.parse_chat(chat_lines())
        tools = [(e["name"], e["detail"]) for e in entries if e["role"] == "tool"]
        self.assertEqual(tools, [("edit", "hello.py"), ("edit", "hello.py"), ("shell", "python3 hello.py")])
        self.assertEqual(len({e["id"] for e in entries if e["role"] == "tool"}), 3)
        self.assertEqual(aider.CHAT["file_tools"], ["edit"])

    def test_aiders_own_output_is_one_event_with_its_lines_of_more_than_one_line(self):
        entries, _ = aider.parse_chat(chat_lines())
        effort = next(e for e in entries if e["role"] == "event" and e["text"].startswith("Set reasoning effort"))
        self.assertIn("Model: openai/fake-model with diff edit format, reasoning high", effort["text"].split("\n"))
        self.assertIn("Restored previous conversation history.", effort["text"].split("\n"))

    def test_a_second_start_in_the_same_log_is_a_resume_divider_with_its_time(self):
        entries, _ = aider.parse_chat(chat_lines())
        div = [e for e in entries if e["role"] == "divider"]
        self.assertEqual([(d["kind"], d["at"][:19]) for d in div], [("resumed", "2026-11-10T23:49:11")])
        self.assertEqual(entries[0]["at"][:19], "2026-11-10T23:45:12")  # each entry has its session's start
        self.assertEqual(entries[-1]["at"][:19], "2026-11-10T23:49:11")

    def test_a_prompt_with_no_reply_yet_is_a_turn_at_work(self):
        lines = chat_lines() + ["", "#### Explain hello.py  "]
        self.assertEqual(aider.parse_chat(lines)[1], "working")
        self.assertEqual(aider.parse_chat(chat_lines() + ["", "#### /ask why  "])[1], "working")
        self.assertIsNone(aider.parse_chat(chat_lines() + ["", "#### /ls  "])[1])
        self.assertEqual(aider.parse_chat([]), ([], None))

    def test_usage_settings_and_effort_from_the_log(self):
        lines = chat_lines()
        self.assertEqual(aider.chat_usage(lines), {"input": 771 + 285 + 813 + 2500 + 2600 + 1000,
                                                   "output": 21 + 5 + 16 + 11 + 16 + 11,
                                                   "cache_read": 0, "cache_write": 0})
        # The cache parts, in the order of Aider's report (base_coder.py): write, then hit.
        self.assertEqual(aider.chat_usage(["> Tokens: 12k sent, 1.5k cache write, 9.8k cache hit, 340 received. "
                                           "Cost: $0.02 message, $0.05 session.  "]),
                         {"input": 12000, "output": 340, "cache_read": 9800, "cache_write": 1500})
        self.assertEqual(aider.chat_settings(lines), {"edit_format": "whole"})  # the resumed session's start
        before = lines[:lines.index("# aider chat started at 2026-11-10 23:49:11")]
        self.assertEqual(aider.chat_settings(before), {"edit_format": "diff"})
        self.assertEqual(aider.read_effort(aider.parse_chat(before)[0]), "high")
        self.assertIsNone(aider.read_effort(aider.parse_chat(lines)[0]))  # the model ignored it after the resume

    def test_the_log_of_a_session_is_found_by_its_name_or_its_path(self):
        self.assertIsNone(self.a.chat_path("w1"))
        log = aider.chat_file("w1")
        log.parent.mkdir(parents=True)
        log.write_text("\n# aider chat started at 2026-11-10 23:45:12\n\n")
        self.assertEqual(self.a.chat_path("w1"), log)
        self.assertEqual(self.a.chat_path(str(FIX / "chat.history.md")), FIX / "chat.history.md")
        self.assertIsNone(self.a.chat_path(""))
        other = self.root / "logs"
        other.mkdir()
        (other / "w2.chat.history.md").write_text("")
        self.assertEqual(self.a.chat_path("w2", None, root=str(other)), other / "w2.chat.history.md")
        self.assertEqual(self.a.chat_ref(), "session")


class SessionsTab(Env):
    """#5222: the Sessions tab reads Aider's Markdown log through parse_chat, a block (a prompt and what follows it)
    at a time, and scrolls up through it by the byte where each block starts."""

    def setUp(self):
        super().setUp()
        self.log = self.root / "chat.history.md"
        self.log.write_bytes((FIX / "chat.history.md").read_bytes())
        sessions._TAIL_CACHE.clear()

    def test_the_tab_shows_the_same_entries_as_parse_chat(self):
        self.assertEqual(self.a.chat_format(), "text")
        whole, _ = aider.parse_chat(chat_lines())
        got = sessions.entries("aider", self.log)
        plain = [{k: v for k, v in e.items() if k != "id"} for e in got["entries"]]
        self.assertEqual(plain, [{k: v for k, v in e.items() if k != "id"} for e in whole])
        ids = [e["id"] for e in got["entries"] if e["role"] == "tool"]
        self.assertEqual(len(set(ids)), 3)  # a call id with the byte of its block: unique in the log
        self.assertEqual((got["before"], got["turn"]), (0, None))
        self.assertEqual(got["cursor"], self.log.stat().st_size)

    def test_a_new_prompt_and_its_reply_come_after_the_cursor(self):
        cur = sessions.entries("aider", self.log)["cursor"]
        with open(self.log, "a") as f:
            f.write("\n#### Explain hello.py  \n")
        got = sessions.entries("aider", self.log, cur)
        self.assertEqual([(e["role"], e["text"]) for e in got["entries"]], [("user", "Explain hello.py")])
        self.assertEqual(got["turn"], "working")
        with open(self.log, "a") as f:
            f.write("\nIt prints a greeting.\n\nRun it with python3.\n")
        got = sessions.entries("aider", self.log, got["cursor"])
        self.assertEqual([(e["role"], e["text"]) for e in got["entries"]],
                         [("assistant", "It prints a greeting.\n\nRun it with python3.")])

    def test_scrolling_up_reads_whole_blocks_back_to_the_start(self):
        whole, _ = aider.parse_chat(chat_lines())
        size = self.log.stat().st_size
        pages, before = [], size
        while True:
            got, before = sessions._page_back("aider", self.log, before, limit=4)
            pages.insert(0, got)
            self.assertTrue(got)
            if before == 0:
                break
        self.assertGreater(len(pages), 2)
        texts = [(e["role"], e["text"]) for p in pages for e in p]
        self.assertEqual(texts, [(e["role"], e["text"]) for e in whole])  # each entry once, none cut in two
        # Each page starts at a prompt: no reply or event is split across two pages.
        data = self.log.read_bytes()
        _, b = sessions._page_back("aider", self.log, size, limit=4)
        self.assertTrue(data[b:].startswith(b"#### "), data[b:b + 20])

    def test_the_tail_of_a_long_log_starts_at_a_block(self):
        whole, _ = aider.parse_chat(chat_lines())
        got = sessions.entries("aider", self.log, tail=600)
        self.assertGreater(got["before"], 0)
        self.assertTrue(self.log.read_bytes()[got["before"]:].startswith(b"#### "))
        tail = [(e["role"], e["text"]) for e in got["entries"]]
        self.assertEqual(tail, [(e["role"], e["text"]) for e in whole][-len(tail):])
        # The resume divider sits in the block before the prompt after it, so a page back from there still has it.
        older, _ = sessions._page_back("aider", self.log, got["before"], limit=1000)
        self.assertEqual(len(older) + len(tail), len(whole))

    def test_the_contract_checks_the_format_and_the_block(self):
        import types
        m = types.SimpleNamespace(DESCRIBE={"id": "x", "label": "X", "exe": "x", "family": "multi"},
                                  CHAT={"format": "csv", "block": "("})
        problems = agents.check(m)
        self.assertTrue(any("CHAT['format']" in p for p in problems), problems)
        self.assertTrue(any("CHAT['block']" in p for p in problems), problems)


class Commands(Env):
    def test_the_built_in_commands_have_the_contracts_shape(self):
        names = [c["name"] for c in self.a.read_commands(str(self.root))]
        self.assertEqual(len(names), len(set(names)))
        for n in ("add", "drop", "ask", "code", "architect", "run", "undo", "model", "reasoning-effort", "exit"):
            self.assertIn(n, names)
        self.assertTrue(all(not n.startswith("/") for n in names))
        self.assertTrue(all(c["description"] for c in aider.COMMANDS))

    def test_setup_names_agents_md(self):
        self.assertEqual(aider.SETUP["instructions"], "AGENTS.md")
        self.assertIsNone(getattr(aider, "write_mcp_config", None))  # Aider has no MCP client


if __name__ == "__main__":
    unittest.main()
