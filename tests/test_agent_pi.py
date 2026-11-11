"""The Pi adapter, river/agents/pi.py (#5170), against the session files in tests/fixtures/pi."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import agents, core
from river.agents import pi

FIX = Path(__file__).resolve().parent / "fixtures" / "pi"
SESSION_ID = "7f3c2a10-5b9e-4c1d-9a2f-0e6d4b8c1a21"


def lines(name="session.jsonl"):
    return (FIX / name).read_text().splitlines()


class Env(unittest.TestCase):
    """A temporary queue (MAXPM_DB), home and Pi folder, so no test reads or writes the person's own files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "home").mkdir()
        env = mock.patch.dict(os.environ, {"MAXPM_DB": str(self.root / "t.db"), "HOME": str(self.root / "home"),
                                           "PI_CODING_AGENT_DIR": str(self.root / "pi-agent")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PI_CODING_AGENT_SESSION_DIR", None)
        agents.load(refresh=True)
        self.addCleanup(lambda: agents.load(refresh=True))
        self.a = agents.get("pi")


class Describe(Env):
    def test_it_is_a_built_in_adapter_with_every_part_but_detect(self):
        self.assertEqual(agents.check(pi, "pi"), [])
        self.assertEqual((self.a.source, self.a.missing()), ("builtin", []))
        self.assertIs(agents.get("pi-coding-agent"), self.a)
        for part in ("describe", "settings", "effort", "launch", "identity", "chat", "state", "input", "resume",
                     "commands", "setup"):
            self.assertTrue(self.a.supports(part), part)
        self.assertFalse(self.a.supports("detect"))  # the extension is the one source of the state
        self.assertEqual(self.a.describe["family"], "multi")
        self.assertEqual(self.a.config_key("tools"), "pi_tools")

    def test_a_command_inside_pi_is_found_by_its_env_even_from_a_claude_code_shell(self):
        self.assertIs(agents.from_env({"PI_CODING_AGENT": "true"}), self.a)
        self.assertIs(agents.from_env({"PI_CODING_AGENT": "true", "CLAUDECODE": "1"}), self.a)
        self.assertIsNone(agents.from_env({"PI_CODING_AGENT": ""}))

    def test_each_maxpm_effort_is_a_pi_level_of_the_same_name(self):
        """Pi 1.x has max (0.73.1 stopped at xhigh) and /thinking <level> in a running session (#5204)."""
        self.assertEqual([self.a.effort_name(x) for x in agents.MAXPM_EFFORTS], list(agents.MAXPM_EFFORTS))
        self.assertTrue(set(agents.MAXPM_EFFORTS) <= set(pi.EFFORT["levels"]))
        self.assertEqual(pi.EFFORT["live"].format(value="max"), "/thinking max")

    def test_the_built_in_commands_have_names_and_no_slash(self):
        names = [c["name"] for c in self.a.read_commands(None)]
        self.assertIn("compact", names)
        self.assertIn("resume", names)
        self.assertFalse(any(n.startswith("/") for n in names))
        self.assertTrue({"thinking", "trust", "bug", "mcp"} <= set(names))  # Pi 1.5.2 (#5204)
        self.assertEqual(len(names), len(set(names)))


class Launch(Env):
    def test_build_command_puts_each_part_on_its_flag(self):
        argv = self.a.build_command("w1", "/work/demo", "anthropic/claude-opus-5-5", "max",
                                    {"tools": "read,bash", "offline": "on", "args": "--no-skills --verbose"}, "go")
        self.assertEqual(argv, ["pi", "--model", "anthropic/claude-opus-5-5", "--thinking", "max", "--tools",
                                "read,bash", "--offline", "--no-skills --verbose", "go"])
        self.assertIsInstance(argv[-2], agents.Raw)  # the person's own shell text goes in as it is
        self.assertEqual(self.a.build_command("w1", None, None, None, {}, None), ["pi"])

    def test_the_skill_goes_into_the_system_prompt_as_a_file(self):
        argv = self.a.build_command("w1", None, "opus", None, {"system_prompt_file": True}, "go")
        i = argv.index("--append-system-prompt")
        f = Path(argv[i + 1])
        self.assertTrue(f.is_file())
        self.assertIn("# The maxpm skill (already loaded)", f.read_text())
        self.assertEqual(f.parent, Path(core.river_dir()) / "prompts")

    def test_fork_and_resume(self):
        self.assertEqual(self.a.build_command("w", None, None, None, {"fork": "7f3c"}, None), ["pi", "--fork", "7f3c"])
        self.assertEqual(self.a.resume_command(SESSION_ID), ["pi", "--session", SESSION_ID])
        self.assertEqual(self.a.resume_command(SESSION_ID, "sonnet", "high", {"prompt": "go", "offline": "on"}),
                         ["pi", "--model", "sonnet", "--thinking", "high", "--offline", "--session", SESSION_ID])
        self.assertIsNone(self.a.resume_command(""))

    def test_parse_command_reads_an_old_command_back(self):
        words = ["/opt/homebrew/bin/pi", "--model", "{model}", "--thinking", "{effort}", "-t", "read", "--offline",
                 "run maxpm go"]
        got = self.a.parse_command(words)
        self.assertEqual(got, {"settings": {"tools": "read", "offline": "on"}, "prompt": "run maxpm go"})
        argv = self.a.build_command("w", None, "{model}", None, got["settings"], got["prompt"])
        self.assertEqual(self.a.parse_command(argv), {"settings": got["settings"], "prompt": "run maxpm go"})

    def test_parse_command_refuses_what_a_profile_cannot_carry(self):
        self.assertIsNone(self.a.parse_command(["claude", "go"]))
        self.assertIsNone(self.a.parse_command(["pi", "-p", "go"]))
        self.assertIsNone(self.a.parse_command(["pi", "--api-key", "x", "go"]))
        self.assertIsNone(self.a.parse_command(["pi", "--model", "sonnet", "go"]))  # a fixed model: not a profile
        self.assertIsNone(self.a.parse_command(["pi", "go", "and more"]))
        self.assertIsNone(self.a.parse_command(["pi"]))


class Chat(Env):
    def test_the_chat_is_the_active_branch_in_the_shared_entries(self):
        entries, turn = self.a.parse_chat(lines())
        roles = [e["role"] for e in entries]
        self.assertEqual(roles, ["event", "event", "user", "assistant", "tool", "result", "summary", "tool", "result",
                                 "divider", "summary", "tool", "result", "event", "assistant"])
        self.assertEqual(turn, "idle")
        text = json.dumps(entries)
        self.assertNotIn("left on another branch", text)  # the branch /tree left
        self.assertNotIn("I run the briefing first", text)  # thinking stays out
        user = entries[2]
        self.assertEqual(user["text"], "run maxpm go in this folder and follow the briefing")
        self.assertEqual(user["images"], ["data:image/png;base64,iVBORw0KGgo="])
        self.assertEqual(entries[4], {"at": "2026-11-11T08:00:02.000Z", "role": "tool", "text": "bash",
                                      "id": "toolu_01", "name": "bash", "detail": "maxpm go"})
        self.assertEqual((entries[5]["for"], entries[5]["error"]), ("toolu_01", False))
        self.assertEqual((entries[7]["detail"], entries[8]["error"]), ("notes.md", True))
        self.assertEqual(entries[9]["kind"], "compacted")
        self.assertEqual((entries[11]["detail"], entries[12]["text"]), ("git status", "nothing to commit"))
        self.assertEqual(entries[0]["text"], "model: anthropic/claude-opus-5-5")
        for e in entries:
            self.assertIn(e["role"], agents.ENTRY_ROLES)

    def test_the_turn_follows_the_last_message(self):
        head = lines()[:-1]
        row = lambda m: json.dumps({"type": "message", "id": "z1", "parentId": "a0000014",
                                    "timestamp": "2026-11-11T08:01:00.000Z", "message": m})
        self.assertEqual(self.a.parse_chat(head + [row({"role": "user", "content": "more"})])[1], "working")
        self.assertEqual(self.a.parse_chat(head + [row({"role": "assistant", "content": [], "stopReason": "toolUse"})])[1],
                         "working")
        self.assertEqual(self.a.parse_chat(head + [row({"role": "assistant", "content": [], "stopReason": "aborted"})]),
                         (self.a.parse_chat(head)[0] + [{"at": "2026-11-11T08:01:00.000Z", "role": "event",
                                                         "text": "interrupted"}], "idle"))
        self.assertEqual(self.a.parse_chat([]), ([], None))
        # Pi 1.x writes a system message when the tools change: it says nothing of the turn (#5204).
        system = json.dumps({"type": "message", "id": "z2", "parentId": "z1", "timestamp": "2026-11-11T08:01:01.000Z",
                             "message": {"role": "system", "content": "", "toolsRemoved": [{"name": "write"}]}})
        self.assertEqual(self.a.parse_chat(head + [row({"role": "user", "content": "more"}), system])[1], "working")

    def test_title_cwd_usage_and_effort(self):
        ls = lines()
        self.assertEqual(pi.chat_title(ls), "maxpm-pi-1")
        self.assertEqual(pi.chat_cwd(ls), "/work/demo")
        # Four model calls, one on the branch that /tree left: each one was billed.
        self.assertEqual(pi.chat_usage(ls), {"input": 4800, "output": 320, "cache_read": 36000, "cache_write": 1600})
        # Pi 1.x: a usage entry is a call with no message (a cache warm), billed too (#5204).
        warm = json.dumps({"type": "usage", "id": "u1", "parentId": "a0000014", "timestamp": "2026-11-11T08:02:00.000Z",
                           "kind": "cache_warm", "usage": {"input": 0, "output": 0, "cacheRead": 50000, "cacheWrite": 0}})
        self.assertEqual(pi.chat_usage(ls + [warm])["cache_read"], 86000)
        import re
        got = [m.group(1) for m in (re.search(pi.EFFORT["read"], ln) for ln in ls) if m]
        self.assertEqual(got, ["high", "medium"])
        self.assertEqual(pi.read_effort(self.a.parse_chat(ls)[0]), "medium")

    def test_chat_path_finds_a_session_by_id_in_its_folder_or_any(self):
        d = self.root / "pi-agent" / "sessions" / pi.folder_key("/work/demo")
        d.mkdir(parents=True)
        f = d / f"2026-11-11T08-00-00-000Z_{SESSION_ID}.jsonl"
        shutil.copy(FIX / "session.jsonl", f)
        self.assertEqual(pi.folder_key("/work/demo"), "--work-demo--")
        self.assertEqual(self.a.chat_path(SESSION_ID, "/work/demo"), f)
        self.assertEqual(self.a.chat_path("7f3c2a10", "/elsewhere"), f)  # the first characters, any folder
        self.assertEqual(self.a.chat_path(str(f)), f)
        self.assertIsNone(self.a.chat_path("0000ffff", "/work/demo"))
        self.assertIsNone(self.a.chat_path("../x", None))
        with mock.patch.dict(os.environ, {"PI_CODING_AGENT_SESSION_DIR": str(self.root / "none")}):
            self.assertIsNone(self.a.chat_path(SESSION_ID, "/work/demo"))

    def test_the_chain_follows_forks_back_to_the_first_file(self):
        first = self.root / "first.jsonl"
        shutil.copy(FIX / "session.jsonl", first)
        fork = self.root / "fork.jsonl"
        fork.write_text((FIX / "fork.jsonl").read_text().replace("PARENT", str(first)))
        self.assertEqual(pi.chat_chain(fork), [first, fork])
        self.assertEqual(pi.chat_chain(first), [first])


class Commands(Env):
    def test_templates_and_skills_of_pi_and_the_folder(self):
        home, proj = self.root / "home", self.root / "proj"
        (proj / ".git").mkdir(parents=True)
        (self.root / "pi-agent" / "prompts").mkdir(parents=True)
        (self.root / "pi-agent" / "prompts" / "review.md").write_text(
            "---\ndescription: Review staged changes\nargument-hint: \"<PR-URL>\"\n---\nReview it.\n")
        (proj / ".pi" / "prompts").mkdir(parents=True)
        (proj / ".pi" / "prompts" / "wr.md").write_text("Finish the task end to end.\n")
        (home / ".agents" / "skills" / "pdf").mkdir(parents=True)
        (home / ".agents" / "skills" / "pdf" / "SKILL.md").write_text("---\nname: pdf-tools\ndescription: PDFs\n---\n")
        (proj / ".agents" / "skills" / "deploy").mkdir(parents=True)
        (proj / ".agents" / "skills" / "deploy" / "SKILL.md").write_text("---\ndescription: Deploy it\n---\n")
        (self.root / "pi-agent" / "skills").mkdir(parents=True)
        (self.root / "pi-agent" / "skills" / "notes.md").write_text("---\ndescription: Notes\n---\n")
        got = {c["name"]: c for c in self.a.read_commands(str(proj))}
        self.assertEqual(got["review"], {"name": "review", "description": "Review staged changes", "hint": "<PR-URL>"})
        self.assertEqual(got["wr"]["description"], "Finish the task end to end.")
        self.assertEqual(got["skill:pdf-tools"]["description"], "PDFs")
        self.assertIn("skill:deploy", got)
        self.assertIn("skill:notes", got)
        self.assertIn("compact", got)  # the built-in ones come first


class State(Env):
    def test_install_hooks_writes_the_extension_beside_the_queue(self):
        report = ["maxpm", "report"]
        words = self.a.install_hooks("/work/demo", report)
        f = Path(core.river_dir()) / "pi" / pi.EXTENSION_NAME
        self.assertEqual(words, ["-e", str(f)])
        text = f.read_text()
        self.assertIn('const REPORT: string[] = ["maxpm", "report"];', text)
        self.assertIn(json.dumps(str(f)), text)
        for event in ("session_start", "agent_start", "agent_end", "session_shutdown"):
            self.assertIn(f'pi.on("{event}"', text)
        before = f.stat().st_mtime_ns
        self.assertEqual(self.a.install_hooks("/other", report), words)
        self.assertEqual(f.stat().st_mtime_ns, before)  # the same text is not written again
        self.assertFalse((self.root / "home" / ".pi").exists())  # nothing in the person's own Pi folder

    @unittest.skipUnless(shutil.which("node"), "no node")
    def test_the_extension_reports_each_state_in_order(self):
        """Run the extension in node with a stand-in for Pi's ExtensionAPI, and fire Pi's events at it."""
        f = Path(self.a.install_hooks("/work/demo", ["maxpm", "--as", "w1", "report"])[1])
        harness = self.root / "harness.mjs"
        harness.write_text("""
            const ext = (await import(process.argv[2])).default;
            const calls = [], handlers = {};
            let name;
            const pi = {
              on: (e, h) => { handlers[e] = h; },
              exec: (cmd, args) => { calls.push([cmd, ...args]); return Promise.resolve({ code: 0 }); },
              getSessionName: () => name, setSessionName: (n) => { name = n; },
            };
            const ctx = { sessionManager: { getSessionId: () => "7f3c" } };
            ext(pi);
            await handlers.session_start?.({ reason: "startup" }, ctx);
            await handlers.agent_start?.({}, ctx);
            await handlers.agent_end?.({ messages: [] }, ctx);
            await handlers.session_shutdown?.({ reason: "reload" }, ctx);
            await handlers.session_shutdown?.({ reason: "quit" }, ctx);
            console.log(JSON.stringify({ calls, name }));
        """)
        env = {**os.environ, "MAXPM_AGENT": "w1"}
        out = subprocess.run(["node", str(harness), f.as_uri()], capture_output=True, text=True, env=env, timeout=60)
        if out.returncode and "Unknown file extension" in out.stderr:
            self.skipTest("this node does not strip TypeScript types")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got["name"], "w1")
        calls = got["calls"]
        self.assertEqual([c[:6] for c in calls[:3]], [["maxpm", "--as", "w1", "report", "--state", s]
                                                      for s in ("idle", "working", "idle")])
        seqs = [int(c[c.index("--seq") + 1]) for c in calls[:3]]
        self.assertEqual(seqs, sorted(set(seqs)))
        resume = calls[0][calls[0].index("--") + 1:]
        self.assertEqual(resume, ["pi", "-e", str(f), "--session", "7f3c"])
        self.assertEqual(calls[0][calls[0].index("--session-id") + 1], "7f3c")
        self.assertEqual(calls[3:], [["maxpm", "--as", "w1", "release"]])  # only a quit ends it
        out = subprocess.run(["node", str(harness), f.as_uri()], capture_output=True, text=True, timeout=60,
                             env={k: v for k, v in os.environ.items() if k != "MAXPM_AGENT"})
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1])["calls"], [])  # not started by MaximizePM


if __name__ == "__main__":
    unittest.main()
