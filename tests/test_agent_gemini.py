"""The Gemini CLI adapter, river/agents/gemini.py (#5174), against fixtures in tests/fixtures/gemini (see its README)."""
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import agents, core
from river.agents import gemini
from river.core import RiverError

FIX = Path(__file__).parent / "fixtures" / "gemini"
DONE = FIX / "session-2026-11-11T07-50-a64cec76.jsonl"
WORKING = FIX / "session-2026-11-11T07-55-5d0c1b2a.jsonl"
CANCELLED = FIX / "session-cancelled.jsonl"
REAL = FIX / "session-2026-11-14T00-15-006c1604.jsonl"


def lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def screen(text):
    """What maxpm serve's pane watcher (server.watch_prompts) reads from a screen with Gemini's DETECT: the prompt
    parts of a blocked screen, else working or idle."""
    from river import server
    ad = agents.get("gemini")
    tail = server._prompt_tail(server._plain(text))
    parts = server._prompt_found(tail, server._AnyOf(ad.detect["blocked"]), server._layout(ad.detect))
    return ("blocked", parts) if parts else (server._screen_state(tail, ad.detect), None)


class Home(unittest.TestCase):
    """A temporary GEMINI_CLI_HOME (Gemini's folder is <it>/.gemini) and a temporary queue."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        env = mock.patch.dict(os.environ, {"GEMINI_CLI_HOME": str(self.home), "MAXPM_DB": str(self.home / "q.db")})
        env.start()
        self.addCleanup(env.stop)
        self.queue = core.river_dir()


class Contract(Home):
    def test_it_is_registered_and_passes_the_check(self):
        a = agents.get("gemini")
        self.assertIs(agents.get("gemini-cli"), a)
        self.assertEqual(agents.check(gemini, "gemini"), [])
        self.assertEqual(a.missing(), [])
        for part in ("describe", "settings", "launch", "identity", "chat", "state", "detect", "input", "resume",
                     "commands", "setup"):
            self.assertTrue(a.supports(part), part)
        self.assertFalse(a.supports("effort"))
        self.assertIsNone(a.effort_name("high"))
        self.assertEqual(a.describe["family"], "multi")
        self.assertEqual(a.config_key("approval_mode"), "gemini_approval_mode")

    def test_a_command_inside_gemini_is_known_by_its_variable(self):
        self.assertIs(agents.from_env({"GEMINI_CLI": "1"}), agents.get("gemini"))
        # Gemini started from a Claude Code shell keeps CLAUDECODE: Gemini still wins.
        self.assertIs(agents.from_env({"GEMINI_CLI": "1", "CLAUDECODE": "1"}), agents.get("gemini"))
        self.assertIs(agents.from_env({"CLAUDECODE": "1"}), agents.get("claude"))


class Launch(Home):
    def test_a_start_with_every_setting(self):
        argv = agents.get("gemini").build_command(
            "w1", "/p", "pro", "high", {"approval_mode": "auto_edit", "sandbox": "on", "skip_trust": "on",
                                        "args": "--debug -e my-ext"}, "maxpm go")
        self.assertEqual(argv, ["gemini", "--model", "pro", "--approval-mode", "auto_edit", "--sandbox", "--skip-trust",
                                "--include-directories", self.queue, "--debug -e my-ext",
                                "--prompt-interactive", "maxpm go"])
        self.assertIsInstance(argv[9], agents.Raw)

    def test_a_bare_start_lets_gemini_choose(self):
        argv = gemini.build_command(None, None, None, None, {"sandbox": "off", "approval_mode": ""}, None)
        self.assertEqual(argv, ["gemini", "--include-directories", self.queue])

    def test_resume_takes_the_session_id_and_a_first_prompt(self):
        argv = agents.get("gemini").resume_command("a64cec76-7a53-4be3-aa01-16aa3c4060ce", "flash", None,
                                                   {"approval_mode": "yolo", "prompt": "go on"})
        self.assertEqual(argv, ["gemini", "--resume", "a64cec76-7a53-4be3-aa01-16aa3c4060ce", "--model", "flash",
                                "--approval-mode", "yolo", "--include-directories", self.queue,
                                "--prompt-interactive", "go on"])
        self.assertIsNone(gemini.resume_command("", None, None, {}))

    def test_a_hand_written_command_reads_back_into_a_profile(self):
        p = agents.get("gemini").parse_command(
            ["gemini", "-m", "{model}", "--approval-mode", "plan", "-s", "--skip-trust", "--include-directories",
             "{maxpm_dir}", "-i", "maxpm go"])
        self.assertEqual(p, {"settings": {"approval_mode": "plan", "sandbox": "on", "skip_trust": "on"},
                             "prompt": "maxpm go"})
        self.assertEqual(gemini.parse_command(["/usr/local/bin/gemini", "--yolo", "maxpm go"]),
                         {"settings": {"approval_mode": "yolo"}, "prompt": "maxpm go"})
        self.assertEqual(gemini.parse_command(["gemini", "--approval-mode=auto_edit", "--include-directories",
                                               self.queue, "maxpm go"])["settings"], {"approval_mode": "auto_edit"})

    def test_a_command_a_profile_cannot_build_reads_back_as_none(self):
        for words in (["claude", "go"], ["gemini", "--approval-mode", "never", "go"], ["gemini", "--debug", "go"],
                      ["gemini", "--include-directories", "/elsewhere", "go"], ["gemini"], ["gemini", "a; rm -rf x"]):
            self.assertIsNone(gemini.parse_command(words), words)

    def test_each_setting_reads_back_from_the_command_it_builds(self):
        argv = gemini.build_command(None, None, None, None, {"approval_mode": "auto_edit"}, "go")
        spec = next(s for s in gemini.SETTINGS if s["key"] == "approval_mode")
        self.assertEqual(re.search(spec["read"], " ".join(argv)).group(1), "auto_edit")
        self.assertEqual(re.search(spec["read"], "gemini --approval-mode=plan").group(1), "plan")
        model = re.search(gemini.DESCRIBE["model_read"], " ".join(gemini.build_command(None, None, "pro", None, {}, "go")))
        self.assertEqual(model.group(1), "pro")
        self.assertEqual(gemini.DESCRIBE["model_live"].format(value="flash"), "/model set flash")


class Identity(Home):
    def chats(self, slug):
        d = self.home / ".gemini" / "tmp" / slug / "chats"
        d.mkdir(parents=True)
        return d

    def test_the_log_of_a_session_is_found_by_its_id(self):
        d = self.chats("demo")
        shutil.copy(DONE, d / DONE.name)
        other = self.chats("other")
        # A file of another session with the same first 8 characters is not it.
        (other / "session-2026-11-11T08-00-a64cec76.jsonl").write_text(json.dumps({"sessionId": "a64cec76-0000"}) + "\n", encoding="utf-8")
        sid = "a64cec76-7a53-4be3-aa01-16aa3c4060ce"
        self.assertEqual(gemini.chat_path(sid), d / DONE.name)
        self.assertEqual(agents.get("gemini").chat_path(sid, None, str(self.home / ".gemini" / "tmp")), d / DONE.name)
        self.assertIsNone(gemini.chat_path(sid, None, str(self.home / "elsewhere")))
        self.assertIsNone(gemini.chat_path("ffffffff-0000"))
        self.assertIsNone(gemini.chat_path(""))
        # IDENTITY's glob (under ~, which is GEMINI_CLI_HOME here) finds each new session file.
        self.assertEqual(sorted(Path(p).name for p in self.home.glob(gemini.IDENTITY["log_glob"][2:])),
                         sorted([DONE.name, "session-2026-11-11T08-00-a64cec76.jsonl"]))

    def test_the_folder_of_a_session_picks_its_project_first(self):
        project = self.home / "work" / "demo"
        project.mkdir(parents=True)
        (self.home / ".gemini" / "projects.json").parent.mkdir(parents=True, exist_ok=True)
        (self.home / ".gemini" / "projects.json").write_text(json.dumps({"projects": {str(project): "demo-2"}}), encoding="utf-8")
        mine, other = self.chats("demo-2"), self.chats("demo")
        for d in (mine, other):
            shutil.copy(DONE, d / DONE.name)
        os.utime(mine / DONE.name, (1, 1))  # older, but in the session's own project
        self.assertEqual(gemini.chat_path("a64cec76-7a53-4be3-aa01-16aa3c4060ce", str(project)), mine / DONE.name)
        self.assertEqual(gemini.chat_path("a64cec76-7a53-4be3-aa01-16aa3c4060ce"), other / DONE.name)

    def test_a_command_inside_gemini_takes_the_id_of_its_projects_newest_log(self):
        # #5819: Gemini 0.68 gives its shell only GEMINI_CLI=1; the id comes from the log that the turn wrote.
        project = self.home / "work" / "demo"
        (project / "src").mkdir(parents=True)
        (self.home / ".gemini" / "projects.json").parent.mkdir(parents=True, exist_ok=True)
        (self.home / ".gemini" / "projects.json").write_text(json.dumps({"projects": {str(project): "demo"}}),
                                                             encoding="utf-8")
        chats = self.chats("demo")
        log = chats / REAL.name
        shutil.copy(REAL, log)
        sid = "006c1604-f0af-40ae-b1b5-1f960f71cf3d"
        env = {"GEMINI_CLI": "1", "GEMINI_CLI_HOME": str(self.home)}
        self.assertEqual(gemini.session_from_log(project, env), sid)
        self.assertEqual(gemini.session_from_log(project / "src", env), sid)  # a command after cd: the folder above
        with mock.patch("os.getcwd", return_value=str(project)):
            self.assertEqual(core.session_from_env(env), sid)
            self.assertIsNone(core.session_from_env({"GEMINI_CLI_HOME": str(self.home)}))  # not inside Gemini
        # A stray file with no metadata line (seen after a resume) does not count.
        (chats / "session-2026-11-14T00-30-006c1604.jsonl").write_text(
            json.dumps({"id": "x", "type": "user", "content": []}) + "\n", encoding="utf-8")
        self.assertEqual(gemini.session_from_log(project, env), sid)
        # A log that nobody wrote in the last ten minutes is no turn of this command.
        self.assertIsNone(gemini.session_from_log(project, env, now=log.stat().st_mtime + gemini.LOG_RECENT + 1))
        # Two Gemini sessions write logs in one project: no id, as a wrong one is worse.
        (chats / "session-2026-11-14T00-40-77777777.jsonl").write_text(
            json.dumps({"sessionId": "77777777-0000-4000-8000-000000000000", "kind": "main"}) + "\n", encoding="utf-8")
        self.assertIsNone(gemini.session_from_log(project, env))
        # A folder of no Gemini project, and no projects.json at all.
        self.assertIsNone(gemini.session_from_log(self.home, env))
        (self.home / ".gemini" / "projects.json").unlink()
        self.assertIsNone(gemini.session_from_log(project, env))


class Chat(unittest.TestCase):
    def test_a_finished_session_reads_as_the_page_shows_it(self):
        entries, turn = agents.get("gemini").parse_chat(lines(DONE))
        got = [(e["role"], e["text"]) for e in entries]
        self.assertEqual(got, [
            ("event", "Approval mode set to default."),
            ("user", "Explain @app.py in one line."),
            ("tool", "Shell"), ("result", "1 app.py"),
            ("tool", "ReadFile"), ("result", "File not found."),
            ("assistant", "app.py prints the word hi."),
            ("user", "What is in @.gemini-clipboard/clipboard-1.png ?"),
            ("assistant", "A red square on white."),
            ("event", "[API Error: quota exceeded for this minute]"),
        ])
        self.assertEqual(turn, "idle")
        shell, ok, read, failed = entries[2:6]
        self.assertEqual((shell["id"], shell["name"], shell["detail"]), ("run_shell_command__call_1", "run_shell_command", "wc -l app.py"))
        self.assertEqual((ok["for"], ok["error"]), ("run_shell_command__call_1", False))
        self.assertEqual((read["detail"], failed["error"]), ("/Users/demo/Projects/demo/missing.py", True))
        self.assertEqual(entries[7]["images"], ["data:image/png;base64,iVBORw0KGgo="])
        self.assertNotIn("images", entries[1])
        self.assertEqual(entries[-1]["level"], "error")
        for e in entries:
            self.assertIn(e["role"], agents.ENTRY_ROLES)
            self.assertTrue(e["at"].startswith("2026-11-11T07:5"))

    def test_a_reply_that_asked_for_tools_is_a_working_turn(self):
        # Gemini 0.68 writes the tool calls of a reply only when they are done: while they run, the reply's line has
        # no text and no calls.
        entries, turn = gemini.parse_chat(lines(WORKING))
        self.assertEqual([e["role"] for e in entries], ["user"])
        self.assertEqual(turn, "working")
        self.assertEqual(gemini.parse_chat(lines(WORKING)[:2]), ([{"at": "2026-11-11T07:55:01.000Z", "role": "user",
                                                                   "text": "Run the tests."}], "working"))

    def test_a_cancelled_tool_call_ends_the_turn(self):
        entries, turn = gemini.parse_chat(lines(CANCELLED))
        self.assertEqual([(e["role"], e.get("error")) for e in entries], [("user", None), ("tool", None), ("result", True)])
        self.assertEqual(turn, "idle")

    def test_line_by_line_each_reply_and_each_tool_call_shows_once(self):
        got, turn = [], None
        for line in lines(DONE):
            e, st = agents.get("gemini").parse_chat_line(json.loads(line))
            got += [(x["role"], x["text"]) for x in e]
            turn = st or turn
        # The rewound prompt and its reply stay (a line never takes back what is shown); the patch is not applied.
        self.assertEqual(got, [
            ("event", "Approval mode set to default."),
            ("user", "Explain @app.py in one line."),
            ("tool", "Shell"), ("result", "1 app.py"), ("tool", "ReadFile"), ("result", "File not found."),
            ("assistant", "app.py prints the word hi."),
            ("user", "Delete everything."), ("assistant", "I will not do that."),
            ("user", "What is in @.gemini-clipboard/clipboard-1.png ?"),
            ("assistant", "A red square."),
            ("event", "[API Error: quota exceeded for this minute]"),
        ])
        self.assertEqual(turn, "idle")

    def test_the_sessions_tab_reads_a_gemini_log(self):
        from river import sessions
        r = sessions._parse("gemini", DONE.read_bytes(), 0)
        self.assertEqual([e["role"] for e in r["entries"]][:4], ["event", "user", "tool", "result"])
        self.assertEqual(r["turn"], "idle")
        self.assertEqual(len(r["offs"]), len(r["entries"]))

    def test_no_message_yet_is_no_turn_and_bad_lines_are_skipped(self):
        self.assertEqual(gemini.parse_chat(lines(DONE)[:1] + ["not json", "", "[1]"]), ([], None))

    def test_title_folder_and_tokens(self):
        self.assertEqual(gemini.chat_title(lines(DONE)), "Explain app.py and an image")
        self.assertEqual(gemini.chat_title(lines(WORKING)), "Run the tests.")
        self.assertEqual(gemini.chat_cwd(lines(DONE)), "/Users/demo/Projects/demo")
        self.assertIsNone(gemini.chat_cwd(lines(WORKING)))
        # g1 counts once (written twice), g3 is gone with the rewind, g2 and g4 count their cached part apart.
        self.assertEqual(gemini.chat_usage(lines(DONE)), {"input": 14976 + 1400 + 1000, "output": 16 + 297 + 9 + 40 + 4,
                                                          "cache_read": 14000 + 15000, "cache_write": 0})

    def test_the_file_tools_are_gemini_tool_names(self):
        self.assertEqual(gemini.CHAT["file_tools"], ["write_file", "replace"])


class RealLog(unittest.TestCase):
    """The log of a real Gemini CLI 0.68.0 session (#5216): a reply, a turn cancelled with Esc, a read-only shell
    command that ran without a question, an edit the person refused, a shell command the person allowed, then the
    same session resumed with --resume <id> and one more prompt. The model was a scripted local stand-in (the
    README of the fixtures)."""

    SID = "006c1604-f0af-40ae-b1b5-1f960f71cf3d"

    def setUp(self):
        self.lines = lines(REAL)

    def test_the_whole_file_reads_as_gemini_keeps_it(self):
        # Gemini's own $patch lines remove both cancelled turns: the prompt that streamed, and the refused edit.
        entries, turn = gemini.parse_chat(self.lines)
        self.assertEqual([(e["role"], e["text"]) for e in entries], [
            ("user", "say hello"), ("assistant", "Hello. This folder has a small Python file and a README."),
            ("user", "list files here"), ("tool", "Shell"), ("result", "calc.py         README.md"),
            ("assistant", "Done: run_shell_command finished."),
            ("user", "edit the function name"),
            ("user", "make a file please"), ("tool", "Shell"), ("result", ""),
            ("assistant", "Done: run_shell_command finished."),
            ("user", "say hello again"), ("assistant", "Hello. This folder has a small Python file and a README."),
        ])
        self.assertEqual(turn, "idle")
        self.assertEqual([e["detail"] for e in entries if e["role"] == "tool"], ["ls", "touch notes.txt"])

    def test_line_by_line_each_reply_and_each_tool_call_shows_once(self):
        got, turn = [], None
        for line in self.lines:
            e, st = gemini.parse_chat_line(json.loads(line))
            got += [(x["role"], x["text"], x.get("error")) for x in e]
            turn = st or turn
        self.assertEqual(got, [
            ("user", "say hello", None),
            ("assistant", "Hello. This folder has a small Python file and a README.", None),
            ("user", "be slow please", None), ("event", "Request cancelled.", None),
            ("user", "list files here", None), ("tool", "Shell", None), ("result", "calc.py         README.md", False),
            ("assistant", "Done: run_shell_command finished.", None),
            ("user", "edit the function name", None), ("tool", "Edit", None),
            ("result", "[Operation Cancelled] Reason: User denied execution.", True),
            ("event", "Request cancelled.", None),
            ("user", "make a file please", None), ("tool", "Shell", None), ("result", "", False),
            ("assistant", "Done: run_shell_command finished.", None),
            ("user", "say hello again", None),
            ("assistant", "Hello. This folder has a small Python file and a README.", None),
        ])
        self.assertEqual(turn, "idle")

    def test_a_reply_that_asks_for_a_tool_is_working_until_its_calls_are_written(self):
        # Gemini writes the reply with no calls first; while a confirmation waits, that is the last line.
        first = next(json.loads(x) for x in self.lines if '"f448c1b4-' in x and "toolCalls" not in x)
        self.assertEqual(gemini.parse_chat_line(first), ([], "working"))

    def test_an_accepted_edit_shows_its_diff(self):
        tc = {"status": "success", "resultDisplay": {"fileDiff": "--- a\n+++ b\n", "fileName": "calc.py"},
              "result": [{"functionResponse": {"response": {"output": "Successfully modified file"}}}]}
        self.assertEqual(gemini._result_text(tc), "--- a\n+++ b\n")
        self.assertEqual(gemini._result_text({"status": "success", "resultDisplay": [[{"text": "a  "}], [{"text": " "}]]}), "a")

    def test_the_session_is_found_by_its_id_and_resume_writes_the_same_file(self):
        self.assertEqual(json.loads(self.lines[0])["sessionId"], self.SID)
        self.assertEqual(sum('"sessionId"' in x for x in self.lines), 2)  # the first line, and the resume's $set
        with tempfile.TemporaryDirectory() as tmp:
            chats = Path(tmp) / "gemdemo" / "chats"
            chats.mkdir(parents=True)
            shutil.copy(REAL, chats / REAL.name)
            self.assertEqual(gemini.chat_path(self.SID, None, tmp), chats / REAL.name)
        self.assertEqual(gemini.resume_command(self.SID, None, None, {})[:3], ["gemini", "--resume", self.SID])

    def test_title_folder_and_tokens(self):
        self.assertEqual(gemini.chat_title(self.lines), "say hello")
        self.assertEqual(gemini.chat_cwd(self.lines), "/Users/demo/projects/gemdemo")
        # Six model lines stay (the refused edit's reply is removed); each said 1200 in and 20 out.
        self.assertEqual(gemini.chat_usage(self.lines), {"input": 7200, "output": 120, "cache_read": 0, "cache_write": 0})


class Screen(unittest.TestCase):
    def read(self, name):
        return screen((FIX / name).read_text(encoding="utf-8"))

    def test_the_process_is_the_cli_not_its_interpreter(self):
        # 'node /opt/homebrew/bin/gemini' on the pane's terminal: the watcher matches the script's name (#5227).
        self.assertEqual(gemini.DETECT["process"], "gemini")

    def test_each_screen_reads_as_its_state(self):
        # Screens of a real Gemini CLI 0.68.0 in a tmux pane (#5216); pane_cancelled is the screen after Esc.
        for name, want in (("pane_idle.txt", "idle"), ("pane_idle_draft.txt", "idle"), ("pane_working.txt", "working"),
                           ("pane_cancelled.txt", "idle"),
                           ("pane_blocked_shell.txt", "blocked"), ("pane_blocked_edit.txt", "blocked"),
                           ("pane_blocked_trust.txt", "blocked"), ("pane_blocked_auth.txt", "blocked")):
            self.assertEqual(self.read(name)[0], want, name)

    def test_a_blocked_screen_gives_its_question_and_options(self):
        _, p = self.read("pane_blocked_shell.txt")
        self.assertEqual(p["question"], "Allow execution of [Shell]?")
        self.assertEqual(p["options"], [("1. Allow once", True), ("2. Allow for this session", False),
                                        ("3. No, suggest changes (esc)", False)])
        _, p = self.read("pane_blocked_edit.txt")
        self.assertEqual(p["question"], "Apply this change?")
        self.assertEqual([o for o, _ in p["options"]], ["1. Allow once", "2. Allow for this session",
                                                        "3. Modify with external editor", "4. No, suggest changes (esc)"])
        _, p = self.read("pane_blocked_trust.txt")
        self.assertEqual(p["question"], "Do you trust the files in this folder?")
        self.assertEqual(p["options"], [("1. Trust folder (gemdemo)", True), ("2. Trust parent folder (projects)", False),
                                        ("3. Don't trust", False)])
        self.assertEqual(self.read("pane_blocked_auth.txt")[1]["options"], [
            ("1. Sign in with Google", True), ("2. Use Gemini API Key", False), ("3. Vertex AI", False)])

    def test_a_tool_question_names_the_tool_and_what_it_runs(self):
        # #5817: the header line over the question ('? Shell  touch notes.txt'), read with DETECT['header'].
        for name, tool, command in (("pane_blocked_shell.txt", "Shell", "touch notes.txt"),
                                    ("pane_blocked_edit.txt", "Edit", "calc.py: def add(a, b): => def plus(a, b):"),
                                    ("pane_blocked_trust_dirs_tool.txt", "Shell", "touch notes.txt")):
            _, p = self.read(name)
            self.assertEqual((p["tool"], p["command"]), (tool, command), name)
        self.assertEqual(self.read("pane_blocked_trust.txt")[1]["tool"], None)  # a dialog, no tool
        # The alert to the person says what the question runs.
        from river import server
        ad = agents.get("gemini")
        tail = server._prompt_tail(server._plain((FIX / "pane_blocked_shell.txt").read_text(encoding="utf-8")))
        text = server.prompt_alert("g1", "maxpm:1.0", tail, layout=server._layout(ad.detect))[0]
        self.assertIn("It asks to run (Shell): touch notes.txt. Question: Allow execution of [Shell]?", text)

    def test_the_trust_question_of_the_added_queue_folder(self):
        # #5818: real screens of Gemini CLI 0.68.0 started with --skip-trust --include-directories <queue folder>.
        trust = [("1. Yes", True), ("2. Yes, and remember the directories as trusted", False), ("3. No", False)]
        for name in ("pane_blocked_trust_dirs.txt", "pane_blocked_trust_dirs_after_tool.txt"):
            state, p = self.read(name)
            self.assertEqual((state, p["question"], p["options"]),
                             ("blocked", "Do you trust the following folders being added to this workspace?", trust),
                             name)
        # A first prompt's tool question waits above it, and Enter answers the tool question (the command ran, the
        # trust question stayed): the page shows the tool question, so its answer goes where the keys go.
        state, p = self.read("pane_blocked_trust_dirs_tool.txt")
        self.assertEqual((state, p["question"]), ("blocked", "Allow execution of [Shell]?"))
        self.assertEqual(p["options"], [("1. Allow once", True), ("2. Allow for this session", False),
                                        ("3. No, suggest changes (esc)", False)])

    def test_the_tell_instead_option_is_on_each_tool_menu(self):
        for name in ("pane_blocked_shell.txt", "pane_blocked_edit.txt"):
            opts = [o for o, _ in self.read(name)[1]["options"]]
            self.assertTrue(any(re.search(gemini.DETECT["tell_instead"], o) for o in opts), name)

    def test_a_reply_that_ends_in_a_question_is_no_prompt(self):
        text = (FIX / "pane_working.txt").read_text(encoding="utf-8").replace(
            " ⠧ Thinking...", " ✦ Allow execution of the tests now?\n ⠧ Thinking...")
        self.assertIn("Allow execution of the tests now?", text)
        self.assertEqual(screen(text)[0], "working")

    def test_the_state_comes_from_the_screen_and_the_id_from_the_log(self):
        self.assertEqual(gemini.STATE, {"state": "detect", "session_id": "identity"})
        self.assertEqual(gemini.IDENTITY["source"], "log")


class Commands(Home):
    def test_the_built_in_commands_and_the_custom_ones_of_both_folders(self):
        user = self.home / ".gemini" / "commands"
        (user / "git").mkdir(parents=True)
        (user / "git" / "commit.toml").write_text('description = "Write a commit message"\nprompt = "..."\n', encoding="utf-8")
        (user / "review.toml").write_text('prompt = "Review {{args}}"\n', encoding="utf-8")
        project = self.home / "p"
        (project / ".gemini" / "commands").mkdir(parents=True)
        (project / ".gemini" / "commands" / "review.toml").write_text("description = 'Review this project'\nprompt = 'x'\n", encoding="utf-8")
        got = {c["name"]: c for c in agents.get("gemini").read_commands(str(project))}
        self.assertEqual(got["git:commit"]["description"], "Write a commit message")
        self.assertEqual(got["review"]["description"], "Review this project")
        self.assertIn("compress", got)
        self.assertEqual(got["compress"]["description"], "Compresses the context by replacing it with a summary")
        no_project = {c["name"]: c for c in gemini.read_commands(None)}
        self.assertEqual(no_project["review"]["description"], "Custom command from review.toml")
        for c in gemini.COMMANDS:
            self.assertTrue({"name", "description", "hint", "run"} <= set(c) <= set(agents.COMMAND_KEYS), c)  # #5290
            self.assertFalse(c["name"].startswith("/"))

    def test_the_toml_description_reads_without_tomllib_too(self):
        text = 'prompt = "x"\ndescription = "Say \\"hi\\""\n'
        self.assertEqual(gemini._toml_description(text), 'Say "hi"')
        with mock.patch.dict("sys.modules", {"tomllib": None}):
            self.assertEqual(gemini._toml_description(text), 'Say "hi"')
            self.assertEqual(gemini._toml_description("description = 'plain'"), "plain")


class Setup(Home):
    def test_the_mcp_server_goes_into_the_user_settings(self):
        path = self.home / ".gemini" / "settings.json"
        a = agents.get("gemini")
        self.assertIn("added MaximizePM", a.write_mcp_config())
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["maxpm"]
        self.assertEqual(entry["args"][-1], "mcp")
        self.assertEqual(entry["env"], {"MAXPM_DB": str(core.db_path().expanduser().resolve())})  # no MAXPM_CHAT
        self.assertIn("nothing changed", a.write_mcp_config())
        path.write_text(json.dumps({"theme": "Dark", "mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
        a.write_mcp_config()
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((data["theme"], data["mcpServers"]["other"]), ("Dark", {"command": "x"}))
        self.assertTrue(path.with_name("settings.json.bak").exists())
        self.assertIn("took MaximizePM out", a.write_mcp_config(remove=True))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["mcpServers"], {"other": {"command": "x"}})
        self.assertIn("nothing changed", a.write_mcp_config(remove=True))
        other = self.home / "elsewhere.json"
        a.write_mcp_config(other)
        self.assertIn("maxpm", json.loads(other.read_text(encoding="utf-8"))["mcpServers"])

    def test_a_settings_file_with_comments_is_left_as_it_is(self):
        path = self.home / ".gemini" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text('{\n  // mine\n  "theme": "Dark"\n}\n', encoding="utf-8")
        with self.assertRaisesRegex(RiverError, "gemini mcp add --scope user -e MAXPM_DB=.* maxpm .* mcp"):
            agents.get("gemini").write_mcp_config()
        self.assertIn("// mine", path.read_text(encoding="utf-8"))

    def test_the_setup_names_gemini_md(self):
        self.assertEqual(gemini.SETUP["instructions"], "GEMINI.md")



class InitFile(unittest.TestCase):
    """#5218: maxpm init makes GEMINI.md the one line @AGENTS.md (Gemini's own import), so a Gemini CLI session reads
    the work queue block and the rules of AGENTS.md, as Claude Code reads them through CLAUDE.md."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d)

    def test_init_writes_gemini_md_as_an_import_of_agents_md(self):
        from river import cli
        self.assertEqual(gemini.SETUP["import"].format(file="AGENTS.md"), "@AGENTS.md")
        self.assertIn(("GEMINI.md", "@AGENTS.md", "Gemini CLI"), agents.import_files())
        lines = cli.setup_instructions(self.d)
        g = self.d / "GEMINI.md"
        self.assertEqual(g.read_text(encoding="utf-8"), "@AGENTS.md\n")
        self.assertIn("GEMINI.md: created as the one line @AGENTS.md, so Gemini CLI reads AGENTS.md too", lines)
        self.assertEqual(cli.setup_instructions(self.d), ["AGENTS.md: already has the work queue block"])

    def test_a_gemini_md_with_rules_keeps_them_and_gets_the_block(self):
        from river import cli
        g = self.d / "GEMINI.md"
        g.write_text("# Gemini rules\n", encoding="utf-8")
        cli.setup_instructions(self.d)
        self.assertTrue(g.read_text(encoding="utf-8").startswith("# Gemini rules\n"))
        self.assertEqual(g.read_text(encoding="utf-8").count("## Work queue"), 1)
        g.write_text(cli.AGENT_SNIPPET, encoding="utf-8")  # only the block, from an older init: it becomes the import
        cli.setup_instructions(self.d)
        self.assertEqual(g.read_text(encoding="utf-8"), "@AGENTS.md\n")


if __name__ == "__main__":
    unittest.main()
