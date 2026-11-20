"""The Antigravity CLI adapter, river/agents/antigravity.py (#6248), against the screens and the transcript of a real
agy 1.5.13 session in a tmux pane (tests/fixtures/agy, 2026-11-19; the email, the paths and the environment dump
cleaned for a public repository)."""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import agents, core
from river.agents import antigravity

FIX = Path(__file__).parent / "fixtures" / "agy"
LOG = FIX / "transcript_full.jsonl"
CID = "b7154f41-bb60-49c7-b354-59d9330f2863"


def lines(path=LOG):
    return path.read_text(encoding="utf-8").splitlines()


def screen(text):
    """What maxpm serve's pane watcher (server.watch_prompts) reads from a screen with agy's DETECT: the prompt parts
    of a blocked screen, else working or idle."""
    from river import server
    ad = agents.get("antigravity")
    tail = server._prompt_tail(server._plain(text))
    parts = server._prompt_found(tail, server._AnyOf(ad.detect["blocked"]), server._layout(ad.detect))
    return ("blocked", parts) if parts else (server._screen_state(tail, ad.detect), None)


class Home(unittest.TestCase):
    """A temporary HOME (agy's folder is <it>/.gemini/antigravity-cli) and a temporary queue."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "MAXPM_DB": str(self.home / "q.db")})
        env.start()
        self.addCleanup(env.stop)
        self.queue = core.river_dir()


class Contract(Home):
    def test_it_is_registered_and_passes_the_check(self):
        a = agents.get("antigravity")
        self.assertIs(agents.get("agy"), a)
        self.assertIs(agents.by_exe("/opt/homebrew/bin/agy"), a)
        self.assertEqual(agents.check(antigravity, "antigravity"), [])
        self.assertEqual(a.missing(), [])
        for part in ("describe", "settings", "effort", "launch", "identity", "chat", "state", "detect", "input",
                     "resume", "commands", "setup"):
            self.assertTrue(a.supports(part), part)
        self.assertEqual(a.describe["family"], "multi")
        self.assertEqual(a.config_key("mode"), "agy_mode")
        self.assertEqual(a.effort_name("xhigh"), "xhigh")
        # One of the three main agents (mark 2026-11-19), after Claude Code and Codex.
        self.assertEqual(agents.ids()[:3], ["claude", "codex", "antigravity"])

    def test_a_command_inside_agy_is_known_by_its_variables(self):
        env = {"ANTIGRAVITY_AGENT": "1", "ANTIGRAVITY_CONVERSATION_ID": CID}
        self.assertIs(agents.from_env(env), agents.get("antigravity"))
        # agy started from a Claude Code shell keeps CLAUDECODE: agy still wins.
        self.assertIs(agents.from_env({**env, "CLAUDECODE": "1"}), agents.get("antigravity"))
        self.assertEqual(core.session_from_env(env), CID)
        self.assertIsNone(core.session_from_env({"ANTIGRAVITY_AGENT": "1", "ANTIGRAVITY_CONVERSATION_ID": "../x"}))


class Launch(Home):
    def test_a_start_with_every_setting(self):
        argv = antigravity.build_command("w1", "/p", "gemini-3.8-flash", "high",
                                         {"mode": "plan", "skip_permissions": "on", "sandbox": "on",
                                          "args": "--log-file /tmp/x"}, "maxpm go")
        # No --add-dir: agy runs a shell command in a folder that --add-dir names, not in its start folder (#6278).
        allow = ["--settings", '{"permissions":{"allow":["command(maxpm)"],"deny":['
                 '"command(tmux kill-server)","command(tmux -L maximizepm kill-server)",'
                 '"command(tmux -Lmaximizepm kill-server)"]}}']
        self.assertEqual(argv[:-3], ["agy", "--model", "gemini-3.8-flash", "--effort", "high", "--mode", "plan",
                                     "--dangerously-skip-permissions", "--sandbox", *allow])
        self.assertEqual(argv[-3:], ["--log-file /tmp/x", "-i", "maxpm go\n\n" + antigravity.MAXPM_NOTE])
        self.assertIsInstance(argv[-3], agents.Raw)
        self.assertIn("BypassSandbox", antigravity.MAXPM_NOTE)

    def test_the_session_settings_refuse_a_tmux_kill_server_as_core_tmux_guard_does(self):
        """#6251: agy's deny rules, as measured with agy 1.5.13 (a rule matches the first words of each command of a
        chain, after variables and env before it), agree with core.tmux_guard on the forms they cover."""
        rules = json.loads(antigravity.SETTINGS_JSON)["permissions"]
        self.assertEqual(rules["allow"], ["command(maxpm)"])
        self.assertIn(f"command(tmux -L {core.TMUX_SOCKET} kill-server)", rules["deny"])

        def denied(command):
            for part in re.split(r"&&|\|\||;|\|", command):
                words = part.split()
                while words and (words[0] == "env" or re.match(r"[A-Za-z_][A-Za-z0-9_]*=", words[0])):
                    words = words[1:]
                if any(words[:len(r[8:-1].split())] == r[8:-1].split() for r in rules["deny"]):
                    return True
            return False
        for command in ("tmux kill-server", "TMUX_TMPDIR=/tmp/x tmux kill-server", "env A=1 tmux kill-server",
                        "cd /tmp && tmux kill-server", "tmux -L maximizepm kill-server",
                        "tmux -L mx6239 kill-server", "tmux -S /tmp/own.sock kill-server", "tmux ls"):
            with self.subTest(command=command):
                self.assertEqual(denied(command), bool(core.tmux_guard(command)))

    def test_a_bare_start_lets_agy_choose(self):
        allow = ["--settings", antigravity.SETTINGS_JSON]
        self.assertEqual(antigravity.build_command(None, None, None, None, {}, None), ["agy", *allow])
        # An effort that is no agy level and a mode that is no agy mode go nowhere.
        self.assertEqual(antigravity.build_command(None, None, None, "turbo", {"mode": "yolo"}, None), ["agy", *allow])

    def test_resume_takes_the_conversation_id_and_a_first_prompt(self):
        # Measured: 'agy --conversation <id> -i <prompt>' runs the prompt in the resumed chat; agy prints
        # 'agy --conversation=<id>' at its exit (pane_exit.txt).
        argv = antigravity.resume_command(CID, None, None, {"prompt": "go on"})
        self.assertEqual(argv, ["agy", "--conversation", CID, "--settings",
                                antigravity.SETTINGS_JSON, "-i", "go on\n\n" + antigravity.MAXPM_NOTE])
        self.assertIsNone(antigravity.resume_command("", None, None, {}))
        self.assertIn(f"agy --conversation={CID}", (FIX / "pane_exit.txt").read_text(encoding="utf-8"))

    def test_a_hand_written_command_reads_back_into_a_profile(self):
        got = antigravity.parse_command(["agy", "--model", "{model}", "--effort", "{effort}", "--mode=accept-edits",
                                         "--sandbox", "--add-dir", "{maxpm_dir}", "-i", "maxpm go"])
        self.assertEqual(got, {"settings": {"mode": "accept-edits", "sandbox": "on"}, "prompt": "maxpm go"})
        self.assertEqual(antigravity.parse_command(["/opt/homebrew/bin/agy", "--dangerously-skip-permissions",
                                                    "--prompt-interactive", "go"]),
                         {"settings": {"skip_permissions": "on"}, "prompt": "go"})
        # What build_command writes reads back to the same profile.
        argv = antigravity.build_command(None, None, None, None, {"mode": "auto"}, "maxpm go")
        self.assertEqual(antigravity.parse_command(argv), {"settings": {"mode": "auto"}, "prompt": "maxpm go"})

    def test_a_command_a_profile_cannot_build_reads_back_as_none(self):
        for words in (["agy", "-p", "go"], ["agy", "--mode", "yolo", "-i", "go"], ["agy"], ["gemini", "-i", "go"],
                      ["agy", "--add-dir", "/elsewhere", "-i", "go"]):
            self.assertIsNone(antigravity.parse_command(words), words)

    def test_each_setting_reads_back_from_the_command_it_builds(self):
        a = agents.get("antigravity")
        cmd = " ".join(antigravity.build_command(None, None, "gemini-4-neon", "xhigh", {"mode": "auto"}, "go"))
        self.assertEqual(re.search(a.describe["model_read"], cmd).group(1), "gemini-4-neon")
        self.assertEqual(re.search(antigravity.EFFORT["read"], cmd).group(1), "xhigh")
        self.assertEqual(re.search(antigravity.SETTINGS[0]["read"], cmd).group(1), "auto")


class Identity(Home):
    def test_the_transcript_of_a_conversation_is_found_by_its_id(self):
        root = self.home / "brain"
        logs = root / CID / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        self.assertIsNone(antigravity.chat_path(CID, root=root))
        (logs / "transcript.jsonl").write_text("{}\n")
        self.assertEqual(antigravity.chat_path(CID, root=root), logs / "transcript.jsonl")
        (logs / "transcript_full.jsonl").write_text("{}\n")
        self.assertEqual(antigravity.chat_path(CID, root=root), logs / "transcript_full.jsonl")
        # The default folder is agy's own, under HOME.
        self.assertEqual(antigravity.chat_path(CID), None)
        default = self.home / ".gemini" / "antigravity-cli" / "brain" / CID / ".system_generated" / "logs"
        default.mkdir(parents=True)
        (default / "transcript_full.jsonl").write_text("{}\n")
        self.assertEqual(antigravity.chat_path(CID), default / "transcript_full.jsonl")
        for bad in ("", "../../etc", "*"):
            self.assertIsNone(antigravity.chat_path(bad, root=root), bad)


class Chat(unittest.TestCase):
    def test_a_whole_conversation_reads_as_the_page_shows_it(self):
        entries, turn = antigravity.parse_chat(lines())
        self.assertEqual(turn, "idle")
        users = [e["text"] for e in entries if e["role"] == "user"]
        self.assertEqual(users[1], "Change the word hello to goodbye in notes.txt, then run the shell command: "
                                   "touch made.txt")
        self.assertEqual(len(users), 6)
        tools = [(e["name"], e["id"], e["detail"]) for e in entries if e["role"] == "tool"]
        self.assertEqual(tools[1:5], [("run_command", "s6", 'pwd && ls -la && find . -name "notes.txt"'),
                                      ("read_file", "s8", "/Users/me/agyproj/notes.txt"),
                                      ("edit_file", "s11", "/Users/me/agyproj/notes.txt"),
                                      ("run_command", "s12", "touch made.txt && ls -la made.txt")])
        results = {e["for"]: e for e in entries if e["role"] == "result"}
        self.assertEqual(set(t[1] for t in tools), set(results))  # each call has its result
        self.assertEqual(results["s8"]["text"], "Showing lines 1-1 of 1\n1: hello")  # no time stamps
        self.assertFalse(results["s11"]["error"])
        self.assertTrue(results["s12"]["error"])  # the person refused the command
        self.assertIn("user denied permission", results["s12"]["text"])
        self.assertEqual(results["s15"]["text"], "A1: purple")  # the answer to ask_question
        self.assertEqual(tools[5], ("ask_question", "s15", "Which color do you prefer?"))
        self.assertFalse(any("SYSTEM_MESSAGE" in e["text"] for e in entries))

    def test_each_step_has_its_turn(self):
        steps = [json.loads(x) for x in lines()]
        turns = [antigravity.parse_chat_line(d)[1] for d in steps]
        self.assertEqual(turns[:4], ["working", "working", "working", "idle"])
        self.assertIsNone(turns[9])  # agy's own reminder to the model
        # A cancelled turn (Esc) leaves its prompt last: the transcript says working, the screen idle.
        cancelled = next(n for n, d in enumerate(steps) if "400-word story" in d.get("content", ""))
        self.assertEqual(antigravity.parse_chat(lines()[:cancelled + 1])[1], "working")
        self.assertEqual(screen((FIX / "pane_cancelled.txt").read_text(encoding="utf-8"))[0], "idle")

    def test_a_stop_hook_message_is_an_event(self):
        # #6367: the Stop hook's reason (the messages that waited) shows in the chat; agy's own reminders do not.
        pre = ("The following is a <SYSTEM_MESSAGE> not actually sent by the user. It is provided by the system as "
               "important information to pay attention to.\n\n")
        d = {"step_index": 30, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE",
             "created_at": "2026-11-20T05:00:00Z",
             "content": pre + "<SYSTEM_MESSAGE>\nStop hook blocked termination: [maxpm alert from tester] "
                              "look at #12\n</SYSTEM_MESSAGE>"}
        entries, turn = antigravity.parse_chat_line(d)
        self.assertEqual(entries, [{"at": "2026-11-20T05:00:00Z", "role": "event",
                                    "text": "[maxpm alert from tester] look at #12"}])
        self.assertEqual(turn, "working")
        d["content"] = "<SYSTEM_MESSAGE>Stop hook blocked termination: [maxpm note from tester] hi</SYSTEM_MESSAGE>"
        self.assertEqual(antigravity.parse_chat_line(d)[0][0]["text"], "[maxpm note from tester] hi")
        reminder = json.loads(lines()[9])
        self.assertEqual(antigravity.parse_chat_line(reminder), ([], None))

    def test_an_interrupted_turn_error_message_ends_the_turn(self):
        # #6261: Stop (Esc) mid-stream leaves an empty PLANNER_RESPONSE and an ERROR_MESSAGE.
        interrupted = [
            json.dumps({"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-20T05:00:00Z",
                        "content": "<USER_REQUEST>\nWrite 1 to 300.\n</USER_REQUEST>"}),
            json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "2026-11-20T05:00:01Z",
                        "content": ""}),
            json.dumps({"step_index": 2, "type": "ERROR_MESSAGE", "created_at": "2026-11-20T05:00:05Z",
                        "content": "The stream was interrupted"}),
        ]
        self.assertEqual(antigravity.parse_chat(interrupted[:2])[1], "working")
        self.assertEqual(antigravity.parse_chat(interrupted)[1], "idle")

    def test_the_sessions_tab_reads_an_agy_transcript(self):
        from river import sessions
        r = sessions._parse("antigravity", LOG.read_bytes(), 0)
        self.assertEqual([e["role"] for e in r["entries"]][:4], ["user", "tool", "result", "assistant"])
        self.assertEqual(r["turn"], "idle")
        self.assertEqual(len(r["offs"]), len(r["entries"]))

    def test_no_step_yet_is_no_turn_and_bad_lines_are_skipped(self):
        self.assertEqual(antigravity.parse_chat(["not json", "", "[1]", "{}"]), ([], None))

    def test_title_and_tokens(self):
        self.assertTrue(antigravity.chat_title(lines()).startswith("Run this shell command and show its output"))
        u = antigravity.chat_usage(lines())
        steps = [json.loads(x) for x in lines() if '"PLANNER_RESPONSE"' in x]
        self.assertEqual(u, {"input": sum(d["input_tokens"] for d in steps),
                             "output": sum(d["output_tokens"] for d in steps),
                             "cache_read": sum(d["cache_read_tokens"] for d in steps), "cache_write": 0})
        st = {"last": None, "ttl": None, "model": None}
        for raw in LOG.read_bytes().splitlines():
            antigravity.chat_cache_line(raw, st)
        self.assertEqual(st["model"], "gemini-3.8-flash-low")
        last = steps[-1]
        self.assertEqual(st["last"]["context"],
                         last["input_tokens"] + last["cache_read_tokens"] + last["output_tokens"])

    def test_maxpm_usage_reads_each_model_call(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        logs = Path(tmp.name) / CID / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        (logs / "transcript_full.jsonl").write_text(LOG.read_text(encoding="utf-8"), encoding="utf-8")
        requests, fork = antigravity.chat_requests(CID, root=tmp.name)
        steps = [json.loads(x) for x in lines() if '"PLANNER_RESPONSE"' in x]
        self.assertFalse(fork)
        self.assertEqual(len(requests), len(steps))
        self.assertEqual(requests["s1"], ("2026-11-20T04:40:52Z", {"input": 9879, "output": 57, "cache_read": 0,
                                                                   "cache_write": 0, "write_1h": 0, "write_5m": 0}))
        self.assertIsNone(antigravity.chat_requests("b7154f41-0000-0000-0000-000000000000", root=tmp.name))

    def test_the_short_transcript_keeps_arguments_as_json_texts(self):
        # transcript.jsonl (the fallback) holds '"\\"touch made.txt\\""'; the detail is the same.
        d = {"step_index": 10, "type": "PLANNER_RESPONSE", "tool_calls": [
            {"name": "run_command", "args": {"CommandLine": "\"touch made.txt\"", "WaitMsBeforeAsync": "5000"}}]}
        self.assertEqual(antigravity.parse_chat_line(d)[0][0]["detail"], "touch made.txt")


class IdleAfterStop(Home):
    """#6261: after Stop (Esc), the page says idle and Settings restart works."""

    def _setup_session(self, name, agent_type, log_lines):
        c = core.connect()
        self.addCleanup(c.close)
        core.register(c, name)
        core.set_agent_type(c, name, agent_type)
        c.execute("UPDATE agents SET pid=?, host=? WHERE name=?", (os.getpid(), core.this_host(), name))
        c.execute("INSERT INTO agent_sessions (agent, session_id, first_seen) VALUES (?,?,?)",
                  (name, CID, core.iso(core.now())))
        c.commit()
        if agent_type == "antigravity":
            p = self.home / ".gemini" / "antigravity-cli" / "brain" / CID / ".system_generated" / "logs" / "transcript_full.jsonl"
        else:
            p = self.home / ".claude" / "projects" / "p" / f"{CID}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        return c, p

    def test_error_message_makes_page_and_settings_restart_see_idle(self):
        from river import server, sessions
        c, _p = self._setup_session("agy1", "antigravity", [
            json.dumps({"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-20T05:00:00Z",
                        "content": "<USER_REQUEST>\nWrite 1 to 300.\n</USER_REQUEST>"}),
            json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "2026-11-20T05:00:01Z",
                        "content": ""}),
            json.dumps({"step_index": 2, "type": "ERROR_MESSAGE", "created_at": "2026-11-20T05:00:05Z",
                        "content": "The stream was interrupted"}),
        ])
        self.assertEqual(sessions.view(c, "agy1")["state"], "idle")
        self.assertEqual(sessions.views(c)[0]["state"], "idle")
        self.assertEqual(server._quiet_session(c, "agy1", None)[1], "antigravity")

    def test_newer_screen_idle_report_overrides_working_turn_for_detect_agent(self):
        from river import server, sessions
        # Cancelled before any model step: transcript ends at USER_INPUT (turn='working').
        c, p = self._setup_session("agy1", "antigravity", [
            json.dumps({"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-20T05:00:00Z",
                        "content": "<USER_REQUEST>\nWrite a 400-word story.\n</USER_REQUEST>"}),
        ])
        # With no idle report, it is working and Settings restart refuses.
        self.assertEqual(sessions.view(c, "agy1")["state"], "working")
        with self.assertRaisesRegex(core.RiverError, "works now"):
            server._quiet_session(c, "agy1", None)

        # Pane watcher reports idle after the transcript write: page says idle and Settings restart succeeds.
        core.report(c, "agy1", "idle")
        self.assertEqual(sessions.view(c, "agy1")["state"], "idle")
        self.assertEqual(sessions.views(c)[0]["state"], "idle")
        self.assertEqual(server._quiet_session(c, "agy1", None)[1], "antigravity")

        # An older idle report (before a newer transcript write) does NOT override a working turn.
        c.execute("UPDATE agents SET activity_at=? WHERE name='agy1'",
                  (core.iso(core.now() - core.timedelta(minutes=5)),))
        c.commit()
        self.assertEqual(sessions.view(c, "agy1")["state"], "working")
        self.assertEqual(sessions.views(c)[0]["state"], "working")
        with self.assertRaisesRegex(core.RiverError, "works now"):
            server._quiet_session(c, "agy1", None)

    def test_hook_agent_working_turn_is_not_overridden_by_report_idle(self):
        from river import sessions
        c, _p = self._setup_session("cc1", "claude", [
            json.dumps({"type": "user", "timestamp": "2026-11-20T05:00:00Z",
                        "message": {"role": "user", "content": "hi"}}),
        ])
        core.report(c, "cc1", "idle")
        self.assertEqual(sessions.view(c, "cc1")["state"], "working")


class Screen(unittest.TestCase):
    def read(self, name):
        return screen((FIX / f"pane_{name}.txt").read_text(encoding="utf-8"))

    def test_each_screen_reads_as_its_state(self):
        for name, want in (("idle", "idle"), ("idle_draft", "idle"), ("idle_after_reply", "idle"),
                           ("after_reject", "idle"), ("after_ask", "idle"), ("after_two", "idle"),
                           ("resumed", "idle"), ("slash_menu", "idle"), ("cancelled", "idle"),
                           ("working", "working"),
                           ("blocked_command", "blocked"), ("blocked_edit", "blocked"), ("blocked_trust", "blocked"),
                           ("blocked_ask", "blocked"), ("ask_two_q2", "blocked"), ("ask_writein_typed", "blocked")):
            self.assertEqual(self.read(name)[0], want, name)
        self.assertIsNone(self.read("exit")[0])  # agy ended: a shell, no state

    def test_a_blocked_screen_gives_its_question_options_and_command(self):
        _, p = self.read("blocked_command")
        self.assertEqual((p["question"], p["tool"], p["command"]),
                         ("Run this command?", "Command", 'pwd && ls -la && find . -name "notes.txt"'))
        self.assertEqual([o for o, _ in p["options"]][::3], ["1. Yes, run command", "4. No, cancel"])
        _, p = self.read("blocked_edit")
        self.assertEqual((p["question"], p["options"]),
                         ("Accept this file edit?", [("1. Yes, accept this change", True),
                                                     ("2. No, reject this change", False)]))
        _, p = self.read("blocked_trust")
        self.assertEqual((p["question"], p["options"]), ("Do you trust the contents of this project?",
                                                         [("Yes, I trust this folder", True), ("No, exit", False)]))
        # A read outside the workspace, seen in the end-to-end run (the footer there also names its tasks).
        _, p = self.read("blocked_access")
        self.assertEqual((p["question"], [o for o, _ in p["options"]][-1]),
                         ("Allow access to this file?", "3. No, deny access"))

    def test_the_tell_instead_option_is_on_each_tool_menu(self):
        for name in ("blocked_command", "blocked_edit", "blocked_access"):
            opts = [o for o, _ in self.read(name)[1]["options"]]
            self.assertEqual(sum(bool(re.search(antigravity.DETECT["tell_instead"], o.split(". ", 1)[1]))
                                 for o in opts), 1, name)

    def test_the_question_dialog_is_read_by_its_form(self):
        from river import server
        ad = agents.get("antigravity")
        lay = server._layout(ad.detect)
        for name, question in (("blocked_ask", "Which color do you prefer?"), ("ask_two_q2", "Which fruits do you like?")):
            tail = server._prompt_tail(server._plain((FIX / f"pane_{name}.txt").read_text(encoding="utf-8")))
            ask = server._ask_read(tail, lay)
            self.assertEqual((ask["form"], ask["question"]), ("antigravity", question), name)

    def test_the_state_comes_from_the_screen_and_the_id_from_the_environment(self):
        self.assertEqual(antigravity.STATE, {"state": "detect", "session_id": "identity"})
        self.assertEqual(antigravity.IDENTITY["source"], "env")


class SessionSettings(Home):
    """#6260: the Settings dialog of an agy session shows the model, effort and mode it runs, also when it started with
    no --model (mark 2026-11-20: 'not known' for all three)."""

    def setUp(self):
        super().setUp()
        self.addCleanup(mock.patch.stopall)
        antigravity.MODELS.reset()
        self.addCleanup(antigravity.MODELS.reset)

    def test_the_chat_log_names_the_model_and_its_level(self):
        lines = (FIX / "transcript_full.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(antigravity.chat_settings(lines), {"model": "gemini-3.8-flash", "effort": "low"})
        change = json.dumps({"type": "USER_INPUT", "content": "<USER_SETTINGS_CHANGE>\nThe user changed setting "
                             "`Model Selection` from Gemini 3.8 Flash (Low) to Gemini 4 Neon (High). No need to comment"})
        self.assertEqual(antigravity.chat_settings(lines + [change]), {"model": "gemini-4-neon", "effort": "high"})

    def test_the_footer_names_the_mode_the_model_and_its_level(self):
        # Each footer as agy 1.5.13 showed it (#6260), for each mode and form of start.
        for footer, want in (
                ("auto · Gemini 4 Neon · high", {"mode": "auto", "model": "gemini-4-neon", "effort": "high"}),
                ("plan · Gemini 4 Neon · high", {"mode": "plan", "model": "gemini-4-neon", "effort": "high"}),
                ("accept-edits · Gemini 4 Argon · low", {"mode": "accept-edits", "model": "gemini-4-argon",
                                                         "effort": "low"}),
                ("auto · Orchestrator (Neon)", {"mode": "auto", "model": "gemini-pro-orchestrator-neon",
                                                "effort": "none"})):
            self.assertEqual(antigravity.screen_settings(["", "esc to cancel" + " " * 60 + footer, ""]), want, footer)
        idle = (FIX / "pane_idle.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(antigravity.screen_settings(idle), {"mode": "auto", "model": "gemini-3.8-flash", "effort": "low"})
        self.assertEqual(antigravity.screen_settings(["nothing here"]), {})

    def test_the_settings_dialog_reads_the_screen_of_a_session_started_with_no_model(self):
        from river import server, sessions
        c = core.connect()
        self.addCleanup(c.close)
        core.register(c, "agy1")
        c.execute("UPDATE agents SET agent_type='antigravity', pid_cmd='agy --add-dir /q -i go' WHERE name='agy1'")
        screen = "\x1b[2m" + " " * 80 + "plan · Gemini 4 Carbon · medium\x1b[0m\n"
        mock.patch.object(server, "terminal_screen", lambda conn, agent: {"text": screen}).start()
        got = {s["key"]: s for s in sessions.session_settings(c, "agy1")["settings"]}
        self.assertEqual({k: (got[k]["value"], got[k]["source"]) for k in ("model", "effort", "mode")},
                         {"model": ("gemini-4-carbon", "screen"), "effort": ("medium", "screen"),
                          "mode": ("plan", "screen")})
        self.assertEqual(got["model"]["labels"]["gemini-4-carbon"], "Gemini 4 Carbon")


class Commands(Home):
    def test_the_built_in_commands_and_the_skills_of_both_folders(self):
        names = [c["name"] for c in antigravity.COMMANDS]
        for n in ("compact", "model", "effort", "resume", "exit", "usage"):
            self.assertIn(n, names)
        by = {c["name"]: c for c in antigravity.COMMANDS}
        self.assertEqual((by["model"]["setting"], by["usage"]["page"], by["compact"]["run"]), ("model", "usage", "now"))
        project = self.home / "proj"
        for root, name, desc in ((self.home / ".gemini" / "config" / "skills", "deploy", "Ship it"),
                                 (project / ".agents" / "skills", "deploy", "Ship this project"),
                                 (self.home / ".gemini" / "antigravity-cli" / "skills", "notes", "")):
            (root / name).mkdir(parents=True)
            front = f"---\nname: {name}\ndescription: {desc}\n---\n" if desc else "no front matter\n"
            (root / name / "SKILL.md").write_text(front)
        got = {c["name"]: c["description"] for c in antigravity.read_commands(project)}
        self.assertEqual(got, {"deploy": "Ship this project", "notes": "Skill notes"})


class Setup(Home):
    def test_the_mcp_server_goes_into_agys_mcp_file(self):
        f = self.home / ".gemini" / "config" / "mcp_config.json"
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps({"mcpServers": {"fs": {"command": "npx", "args": [], "disabled": False}}}))
        self.assertIn("added MaximizePM", antigravity.write_mcp_config())
        data = json.loads(f.read_text())
        self.assertEqual(set(data["mcpServers"]), {"fs", "maxpm"})
        self.assertEqual(data["mcpServers"]["maxpm"]["args"][-1], "mcp")
        self.assertNotIn("MAXPM_CHAT", data["mcpServers"]["maxpm"]["env"])
        self.assertIn("nothing changed", antigravity.write_mcp_config())
        self.assertIn("took MaximizePM out", antigravity.write_mcp_config(remove=True))
        self.assertEqual(set(json.loads(f.read_text())["mcpServers"]), {"fs"})

    def test_agy_reads_agents_md_itself(self):
        a = agents.get("antigravity")
        self.assertEqual(a.setup["instructions"], "AGENTS.md")


class ProjectHooks(Home):
    """#6351: the setting agy_activity_hooks writes MaximizePM's hooks into the project's .agents/hooks.json, in the
    shapes and with the answers that agy 1.5.13 took on 2026-11-20 (a probe hook in a tmux pane)."""

    def setUp(self):
        super().setUp()
        self.c = core.connect()
        self.addCleanup(self.c.close)
        self.folder = self.home / "proj"
        (self.folder / ".git" / "info").mkdir(parents=True)
        core.project_add(self.c, "proj")
        core.project_path(self.c, "proj", str(self.folder))
        self.file = self.folder / ".agents" / "hooks.json"
        self.mine = {"lint": {"PostToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "lint"}]}]}}

    def report(self, payload, env):
        import contextlib
        import io
        from river import cli
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(core, "agent_process", lambda: (None, None)), contextlib.redirect_stdout(out):
            self.assertEqual(cli.run(["report", "--hook", "antigravity"]), 0)
        return json.loads(out.getvalue()) if out.getvalue().strip() else None

    def test_the_setting_is_off_by_default_and_on_writes_the_hooks_beside_the_persons(self):
        self.assertEqual(core.setting(self.c, "agy_activity_hooks"), "off")
        self.assertEqual(core.project_hooks(self.c, "antigravity", str(self.folder)), {"file": None, "on": False})
        self.assertFalse(self.file.exists())  # off: no file is made
        self.file.parent.mkdir()
        self.file.write_text(json.dumps(self.mine))
        core.config_set(self.c, "agy_activity_hooks", "on", project="proj")
        got = core.project_hooks(self.c, "antigravity", str(self.folder))
        self.assertEqual(got, {"file": str(self.file), "on": True})
        data = json.loads(self.file.read_text())
        self.assertEqual(data["lint"], self.mine["lint"])
        ours = data["maximizepm"]
        # Tool events wrap the handler in "hooks" with a matcher; the other events take the handler itself.
        self.assertEqual(ours["PreToolUse"][0]["matcher"], "run_command")
        self.assertTrue(ours["PreToolUse"][0]["hooks"][0]["command"].endswith(" report --hook antigravity"))
        self.assertEqual(ours["Stop"][0]["type"], "command")
        self.assertEqual(ours["PreInvocation"], ours["Stop"])
        self.assertIn("/.agents/hooks.json", (self.folder / ".git" / "info" / "exclude").read_text())
        before = self.file.stat().st_mtime_ns
        core.project_hooks(self.c, "antigravity", str(self.folder))
        self.assertEqual(self.file.stat().st_mtime_ns, before)  # the same text: not written again
        core.config_set(self.c, "agy_activity_hooks", "off", project="proj")
        self.assertEqual(core.project_hooks(self.c, "antigravity", str(self.folder)), {"file": None, "on": False})
        self.assertEqual(json.loads(self.file.read_text()), self.mine)  # only MaximizePM's entry went

    def test_a_file_of_only_maximizepms_hooks_goes_when_off_and_a_bad_file_is_kept(self):
        core.config_set(self.c, "agy_activity_hooks", "on")
        core.project_hooks(self.c, "antigravity", str(self.folder))
        core.config_set(self.c, "agy_activity_hooks", "off")
        core.project_hooks(self.c, "antigravity", str(self.folder))
        self.assertFalse(self.file.parent.exists())
        self.file.parent.mkdir()
        self.file.write_text("[1, 2]")
        core.project_hooks(self.c, "antigravity", str(self.folder))
        self.assertEqual(self.file.read_text(), "[1, 2]")
        core.config_set(self.c, "agy_activity_hooks", "on")
        with self.assertRaisesRegex(core.RiverError, "maxpm config set agy_activity_hooks off"):
            core.project_hooks(self.c, "antigravity", str(self.folder))

    def test_each_event_is_a_hook_report(self):
        a = agents.get("antigravity")
        run = a.read_hook({"hookEventName": "PreToolUse", "conversationId": CID,
                           "toolCall": {"name": "run_command", "args": {"CommandLine": "git push origin main"}}})
        self.assertEqual((run["run"], run["state"], run["session_id"]), ("git push origin main", None, CID))
        self.assertIsNone(a.read_hook({"hookEventName": "PreToolUse", "toolCall": {"name": "view_file"}}))
        first = a.read_hook({"hookEventName": "PreInvocation", "invocationNum": 0, "lastUserInput": "go"})
        later = a.read_hook({"hookEventName": "PreInvocation", "invocationNum": 3, "lastUserInput": "go"})
        self.assertEqual((first["state"], first["prompt"], later["prompt"]), ("working", "go", None))
        stop = a.read_hook({"hookEventName": "Stop", "fullyIdle": True, "continuationActive": False})
        self.assertEqual((stop["state"], stop["waits"], stop["again"]), ("idle", [], False))
        self.assertEqual(a.read_hook({"hookEventName": "Stop", "fullyIdle": False})["waits"], ["a background command"])
        self.assertIsNone(a.read_hook({"hookEventName": "SessionStart"}))

    def test_the_hook_refuses_a_push_and_brings_messages_at_a_turn_end(self):
        core.register(self.c, "agy1")
        core.register(self.c, "boss")
        core.send(self.c, "alert", "Stop: a prerequisite came first", to="agy1", actor="boss")
        env = {"MAXPM_AGENT": "agy1"}
        push = {"hookEventName": "PreToolUse", "conversationId": CID,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "git push origin main"}}}
        got = self.report(push, env)
        self.assertEqual(got["decision"], "deny")
        self.assertIn("push_guard", got["reason"])
        self.assertIsNone(self.report({**push, "toolCall": {"name": "run_command",
                                                            "args": {"CommandLine": "echo hi"}}}, env))
        self.assertEqual(self.c.execute("SELECT activity_event FROM agents WHERE name='agy1'").fetchone()[0], None)
        self.assertIsNone(self.report({"hookEventName": "PreInvocation", "invocationNum": 0, "conversationId": CID,
                                       "lastUserInput": "go"}, env))
        self.assertEqual(self.c.execute("SELECT activity_event FROM agents WHERE name='agy1'").fetchone()[0],
                         "report:working")
        stop = {"hookEventName": "Stop", "conversationId": CID, "fullyIdle": True, "continuationActive": False}
        got = self.report(stop, env)
        self.assertEqual(got["decision"], "continue")
        self.assertIn("[maxpm alert from boss] Stop: a prerequisite came first", got["reason"])
        self.assertEqual(self.c.execute("SELECT activity_event FROM agents WHERE name='agy1'").fetchone()[0],
                         "report:idle")
        self.assertIsNone(self.report(stop, env))  # delivered once: the next turn end brings nothing
        m = self.c.execute("SELECT native_status, delivered_via, read_at FROM messages WHERE to_agent='agy1'").fetchone()
        self.assertEqual((m["native_status"], m["delivered_via"], m["read_at"]), ("sent", "native", None))

    def test_the_hook_does_nothing_in_a_session_maxpm_did_not_start(self):
        core.register(self.c, "agy1")
        core.send(self.c, "alert", "unread", to="agy1", actor="agy1")
        env = {k: v for k, v in os.environ.items() if k not in ("MAXPM_AGENT", core.TOKEN_ENV)}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(self.report({"hookEventName": "Stop", "conversationId": CID, "fullyIdle": True}, {}))
            self.assertIsNone(self.report({"hookEventName": "PreToolUse", "toolCall": {
                "name": "run_command", "args": {"CommandLine": "tmux kill-server"}}}, {}))

    def test_a_start_writes_the_file_in_its_folder(self):
        from river import server
        core.config_set(self.c, "agy_activity_hooks", "on")
        sent = []
        server._open_terminal({"command": "agy -i go", "path": str(self.folder), "platform": "antigravity",
                               "project": "proj"}, {"MAXPM_AGENT": "agy1"}, sent.append, self.c)
        self.assertEqual(len(sent), 1)
        self.assertIn("maximizepm", json.loads(self.file.read_text()))
        # Another agent's start leaves the folder alone.
        self.file.unlink()
        server._open_terminal({"command": "claude go", "path": str(self.folder), "platform": "claude-code"},
                              {"MAXPM_AGENT": "c1"}, sent.append, self.c)
        self.assertFalse(self.file.exists())


if __name__ == "__main__":
    unittest.main()
