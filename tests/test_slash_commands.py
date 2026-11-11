"""#5186: the slash commands of a session (part 10 of an agent adapter, docs/agent-adapters.md) and the "/" list of the
Sessions message box."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import agents, core, server, sessions
from river.agents import slash
from river.core import RiverError
from tests.test_view_sessions import DRIVE_HEAD, TmuxBase, claude_line, drive_page


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class FrontMatter(unittest.TestCase):
    def test_keys_quotes_and_blocks(self):
        meta, body = slash.front_matter('---\ndescription: "Review a PR"\nargument-hint: [pr]\nnote: >\n  two\n  lines\n'
                                        '---\n# Title\nbody\n')
        self.assertEqual(meta, {"description": "Review a PR", "argument-hint": "[pr]", "note": "two lines"})
        self.assertEqual(body, "# Title\nbody")
        self.assertEqual(slash.front_matter("no front matter\n"), ({}, "no front matter\n"))
        self.assertEqual(slash.front_matter("---\nopen: yes\nno end\n")[0], {})  # no closing line: no front matter

    def test_an_entry_without_a_description_takes_the_first_line(self):
        e = slash.entry("fix", {}, "\n## Fix the bug in $ARGUMENTS\nmore", "p:")
        self.assertEqual(e, {"name": "p:fix", "description": "Fix the bug in $ARGUMENTS", "hint": ""})
        self.assertTrue(len(slash.entry("x", {"description": "w " * 400}, "")["description"]) <= slash.DESCRIPTION_MAX)


class Readers(unittest.TestCase):
    """Claude Code: project and user commands and skills, and the enabled plugins; Codex: its prompts."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        t = Path(self.dir.name)
        self.home, self.cfg, self.codex = t / "home", t / "home" / ".claude", t / "home" / ".codex"
        self.repo = self.home / "code" / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.sub = self.repo / "pkg"
        self.sub.mkdir()
        env = {"CLAUDE_CONFIG_DIR": str(self.cfg), "CODEX_HOME": str(self.codex), "HOME": str(self.home)}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)

    def test_claude_reads_each_place_and_the_nearer_one_wins(self):
        write(self.repo / ".claude/commands/ship.md", "---\ndescription: Ship it (repo)\nargument-hint: <target>\n---\n")
        write(self.sub / ".claude/commands/ship.md", "---\ndescription: Ship it (pkg)\n---\n")
        write(self.repo / ".claude/commands/db/migrate.md", "Run the migrations\n")
        write(self.cfg / "commands/mine.md", "My own command\n")
        write(self.cfg / "commands/compact.md", "Shadows a built-in: left out\n")
        write(self.repo / ".claude/skills/lint/SKILL.md", "---\nname: lint\ndescription: Lint the code\n---\n")
        write(self.cfg / "skills/hidden/SKILL.md", "---\ndescription: x\nuser-invocable: false\n---\n")
        write(self.cfg / "skills/noskill/README.md", "no SKILL.md")
        write(self.home / "code/.claude/commands/outside.md", "above the repository: not this session's\n")
        plug = self.home / "plugins/review/1.0"
        write(plug / "commands/pr.md", "---\ndescription: Review a pull request\n---\n")
        write(plug / "skills/style/SKILL.md", "---\nname: style\ndescription: House style\n---\n")
        off = self.home / "plugins/off/1.0"
        write(off / "commands/nope.md", "a plugin that is off\n")
        write(self.cfg / "plugins/installed_plugins.json", json.dumps({"version": 2, "plugins": {
            "review@market": [{"scope": "user", "installPath": str(plug)}],
            "off@market": [{"scope": "user", "installPath": str(off)}]}}))
        write(self.cfg / "settings.json", json.dumps({"enabledPlugins": {"review@market": True, "off@market": True}}))
        write(self.repo / ".claude/settings.local.json", json.dumps({"enabledPlugins": {"off@market": False}}))
        claude = agents.get("claude-code")
        got = {c["name"]: c for c in claude.read_commands(str(self.sub))}
        self.assertEqual(got["ship"]["description"], "Ship it (pkg)")  # the nearer folder wins
        self.assertEqual(got["migrate"]["description"], "Run the migrations")
        self.assertEqual(got["mine"]["description"], "My own command")
        self.assertEqual(got["lint"]["description"], "Lint the code")
        self.assertEqual(got["review:pr"]["description"], "Review a pull request")
        self.assertEqual(got["review:style"]["description"], "House style")
        for name in ("hidden", "noskill", "outside", "off:nope"):
            self.assertNotIn(name, got)
        self.assertIn("compact", got)
        self.assertNotEqual(got["compact"]["description"], "Shadows a built-in: left out")
        names = [c["name"] for c in claude.read_commands(str(self.sub))]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[:len(claude.commands)], [c["name"] for c in claude.commands])  # built-in ones first
        # No folder: the built-in and the user's commands only.
        self.assertNotIn("ship", {c["name"] for c in claude.read_commands(None)})
        self.assertIn("mine", {c["name"] for c in claude.read_commands(None)})

    def test_codex_reads_its_prompts(self):
        write(self.codex / "prompts/draftpr.md", "---\ndescription: Draft a PR\nargument-hint: FILES=<paths>\n---\n")
        got = {c["name"]: c for c in agents.get("codex").read_commands(str(self.repo))}
        self.assertEqual(got["prompts:draftpr"], {"name": "prompts:draftpr", "description": "Draft a PR",
                                                  "hint": "FILES=<paths>"})
        self.assertIn("model", got)

    def test_the_built_in_lists_pass_the_contract(self):
        for mid in ("claude", "codex"):
            a = agents.get(mid)
            self.assertEqual(agents.check(a.module, mid), [])
            self.assertTrue(a.supports("commands"))
            self.assertTrue(all(not c["name"].startswith("/") for c in a.commands))


class Endpoint(TmuxBase):
    """GET /api/session-commands?agent=: the commands of the session's agent, for the folder it runs in."""

    def get(self, agent):
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.headers, out = (f"/api/session-commands?agent={agent}", ("127.0.0.1", 5555),
                                                    {"Host": "localhost:8765"}, {})
        h._send = lambda code, body, ctype=None, headers=None: out.update(code=code, body=body)
        with mock.patch.object(core, "transcripts_root", lambda: self.root):
            h.do_GET()
        self.assertEqual(out["code"], 200)
        return out["body"]

    def test_a_session_gets_its_agents_commands_for_its_folder(self):
        folder = Path(self.dir.name) / "work"
        write(folder / ".claude/commands/deploy-site.md", "---\ndescription: Deploy the site\n---\n")
        self.write(claude_line("user", "hi", cwd=str(folder)))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(Path(self.dir.name) / "cfg")}):
            got = self.get("ag")
        self.assertEqual((got["provider"], got["folder"]), ("claude", str(folder)))
        names = [c["name"] for c in got["commands"]]
        self.assertIn("compact", names)
        self.assertIn("deploy-site", names)
        self.assertEqual(self.get("nobody")["commands"], [])  # no such session: an empty list


class MessageBox(TmuxBase):
    """The "/" list in the message box: it opens on "/", filters, Up and Down move in it, Tab completes, Esc closes,
    and Up recalls an earlier prompt again when it is closed."""

    def setUp(self):
        super().setUp()
        self.pane["screen"] = "❯ \n"

    def test_slash_opens_the_list_and_tab_completes(self):
        sessions.record_prompt(self.c, "ag", "an earlier prompt", "mark")
        cmds = {"agent": "ag", "provider": "claude", "folder": None, "commands": [
            {"name": "compact", "description": "Free up context", "hint": "[instructions]"},
            {"name": "context", "description": "Show the context", "hint": ""},
            {"name": "clear", "description": "Start again", "hint": ""},
            {"name": "review:pr", "description": "Review a <b>pull</b> request", "hint": ""}]}
        driver = DRIVE_HEAD + r'''
const box = () => q("#sessText");
const list = () => q(".slash-list");
const open = () => list() && !list().classList.contains("hidden");
const rows = () => [...document.querySelectorAll(".slash-row")].map(r => r.querySelector(".slash-name").textContent.split(" ")[0]);
const chosen = () => q(".slash-row.on .slash-name").textContent.split(" ")[0];
const type = (v) => { box().value = v; box().setSelectionRange(v.length, v.length); box().dispatchEvent(new Event("input")); };
const key = (k) => {
  const e = new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true });
  box().dispatchEvent(e);
  return e.defaultPrevented;
};
try {
  await until(() => q("#sessTitle").textContent === "ag" && !box().disabled, "the box of ag");
  box().focus();
  type("/");
  await until(open, "the list opens on /");
  check(rows().join() === "/compact,/context,/clear,/review:pr", "every command: " + rows());
  check(!q(".slash-row b"), "the description is text, not HTML");
  type("/co");
  await until(() => rows().join() === "/compact,/context", "filtered: " + rows());
  check(chosen() === "/compact", "the first row is chosen");
  check(key("ArrowDown") && chosen() === "/context", "Down moves in the list");
  check(key("ArrowDown") && chosen() === "/compact", "and wraps");
  check(key("ArrowUp") && chosen() === "/context", "Up moves back");
  check(box().value === "/co", "the text stays while the list moves");
  check(key("Tab"), "Tab is taken");
  check(box().value === "/context " && box().selectionStart === 9, "Tab completes: " + JSON.stringify(box().value));
  check(!open(), "the list closes after Tab");
  type("/pr");
  await until(open, "a part of the name finds a plugin command");
  check(rows().join() === "/review:pr", "by a part of its name: " + rows());
  check(key("Escape") && !open(), "Esc closes the list");
  check(!key("Tab"), "Tab is not taken when the list is closed");
  type("/compact now");
  check(!open(), "no list after the name");
  type("");
  check(key("ArrowUp") && box().value === "an earlier prompt", "the list closed: Up recalls a prompt");
  type("/cl");
  await until(open, "open again");
  q(".slash-row").dispatchEvent(new MouseEvent("click", { bubbles: true }));
  check(box().value === "/clear " && !open(), "a click completes: " + JSON.stringify(box().value));
  type("/com");
  await until(open, "open for Enter");
  key("ArrowDown"); key("ArrowUp");
  key("Enter");
  await until(() => box().value === "", "Enter sends");
  post({ ok: true });
} catch (e) { post({ error: String(e.stack || e) }); }
'''
        drive_page(self, driver, routes={"/api/session-commands?agent=ag": lambda: cmds})
        self.assertEqual(sessions.sent_prompts(self.c, "ag")[0], "/compact")  # Enter after a move: completed, then sent


if __name__ == "__main__":
    unittest.main()


FIXTURES = Path(__file__).parent / "fixtures"


def screen(agent, name):
    return (FIXTURES / agent / name).read_text(encoding="utf-8")


class MenuScreens(unittest.TestCase):
    """#5220: the menus of slash commands, recorded from Codex 0.159.1 and Claude Code 2.1.322 in tmux, read as prompts
    the page's ! dialog answers; Codex's "/" popup over its input box is no prompt."""

    def found(self, agent, name):
        ad = agents.find(agent)
        tail = server._prompt_tail(screen(agent, name))
        return server._prompt_found(tail, server._AnyOf(ad.detect["blocked"]), server._layout(ad.detect))

    def test_codex_menus_give_their_title_their_numbered_options_and_their_keys(self):
        p = self.found("codex", "pane_menu_model.txt")
        self.assertEqual(p["question"], "Select Model and Effort")  # not the start screen's "...easier?" far above
        self.assertEqual([o[0][:3] for o in p["options"]], [f"{n}. " for n in range(1, 9)])  # no title, no key line
        self.assertEqual([o[1] for o in p["options"]].index(True), 2)  # the marker is on 3. GPT-6.1 Terra (current)
        self.assertEqual(p["hint"], "enter select · esc back")
        p = self.found("codex", "pane_menu_effort.txt")
        self.assertEqual((p["question"], len(p["options"]), p["hint"]),
                         ("Select Reasoning Level for GPT-6.1 Terra", 5, "enter default · s session · esc back"))
        p = self.found("codex", "pane_menu_review.txt")
        self.assertEqual((p["question"], p["options"][0]), ("Select a review preset",
                                                            ("1. Review against a base branch  (PR Style)", True)))

    def test_claude_codes_model_menu_is_a_prompt_with_its_title(self):
        p = self.found("claude", "pane_menu_model.txt")
        self.assertEqual(p["question"], "Select model")  # the title over its description line
        self.assertEqual([o[0].split()[0] for o in p["options"]], ["1.", "2.", "3.", "4.", "5.", "▸"])
        self.assertEqual(p["hint"], "Enter to set as default · s to use this session only · Esc to cancel")

    def test_the_slash_popup_of_codex_is_no_prompt(self):
        # '› /model  choose what model ...' over '› /mod': the input box and its popup (mark 2026-11-11: the page said
        # it waited at a permission prompt).
        self.assertIsNone(self.found("codex", "pane_slash_popup.txt"))


class SentFromThePage(TmuxBase):
    """#5220: a slash command from the message box: its text, a pause (Codex reads a fast Enter as part of a paste),
    then Enter; then a look at the pane says what it did, since no chat log line comes for it."""

    def setUp(self):
        super().setUp()
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET agent_type='codex' WHERE name='ag'")
        self.pane["keys"].clear()

    def send(self, text, shown):
        self.pane["screen"] = shown
        slept = []
        with mock.patch.object(server, "ENTER_PAUSE", 0.25), mock.patch.object(server.time, "sleep", slept.append):
            r = server.OPS["session_send"](self.c, {"agent": "ag", "text": text}, "mark")
        return r, slept

    def test_the_text_then_a_pause_then_enter(self):
        r, slept = self.send("/review", screen("codex", "pane_menu_review.txt"))
        self.assertEqual(self.pane["keys"], ["/review", "Enter"])
        self.assertEqual(slept[0], 0.25)  # between the text and its Enter
        self.assertEqual(r["command"], {"name": "review", "run": "menu", "taken": "menu"})

    def test_a_command_still_in_the_input_box_was_not_taken(self):
        r, _ = self.send("/status", "• Model changed\n\n› /status\n\n  GPT-6.1 Terra medium · ~/code/demo\n")
        self.assertEqual(r["command"], {"name": "status", "run": "terminal", "taken": "waiting"})

    def test_a_command_that_left_an_empty_box_ran(self):
        r, _ = self.send("/compact", "• Context compacted · 2s\n\n› Ask Codex to do anything\n\n  GPT-6.1 Terra medium\n")
        self.assertEqual(r["command"], {"name": "compact", "run": "now", "taken": "ran"})
        r, _ = self.send("/prompts:mine", "› Ask Codex to do anything\n")  # a custom command: its run is not known
        self.assertEqual(r["command"]["run"], None)
        r, _ = self.send("please go on", "› Ask Codex to do anything\n")  # a message is no command
        self.assertNotIn("command", r)
        r, _ = self.send("/ not a command", "› Ask Codex to do anything\n")
        self.assertNotIn("command", r)

    def test_a_command_that_the_settings_dialog_does_goes_nowhere_without_an_argument(self):
        # mark 2026-11-11: /model and /effort with no argument open the session's Settings; their pickers would wait in
        # the terminal for keys (Claude Code saved a preselected effort as the default).
        for agent_type, text in (("codex", "/model"), ("claude-code", "/effort"), ("claude-code", "/model ")):
            with core.tx(self.c):
                self.c.execute("UPDATE agents SET agent_type=? WHERE name='ag'", (agent_type,))
            with self.assertRaisesRegex(RiverError, "opens a picker in the terminal: change the (model|effort) in the "
                                                    "session's Settings"):
                self.send(text, "❯ \n")
        self.assertEqual(self.pane["keys"], [])
        r, _ = self.send("/effort high", "❯ \n")  # with its argument it goes as typed
        self.assertEqual((self.pane["keys"], r["command"]["name"]), (["/effort high", "Enter"], "effort"))

    def test_a_menu_that_closed_is_no_prompt_on_the_page(self):
        # The watch of maxpm serve recorded the menu as blocked; the pane now shows the input box again. For Codex,
        # whose state comes from its pane, the page reads the pane at once and says idle, not 'waits at a prompt'.
        core.report(self.c, "ag", "blocked", "Select Model and Effort")
        self.pane["screen"] = screen("codex", "pane_slash_popup.txt")
        self.assertNotEqual(self.cards()[0]["state"], "prompt")
        v = sessions.view(self.c, "ag", None, self.root, self.croot, prompt=None, pane=True)
        self.assertNotEqual(v["state"], "prompt")
        self.pane["screen"] = screen("codex", "pane_menu_model.txt")  # the menu shows: a prompt
        self.assertEqual(self.cards()[0]["state"], "prompt")
        # An agent whose hooks report its prompts (Claude Code) keeps the hook's word.
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET agent_type='claude-code' WHERE name='ag'")
        self.assertEqual(sessions.view(self.c, "ag", None, self.root, self.croot, prompt=None, pane=True)["state"],
                         "prompt")


class CommandRuns(unittest.TestCase):
    """#5220: each built-in command of Claude Code and Codex says how it works from the page."""

    def test_claude_and_codex_mark_every_command(self):
        from river.agents import claude, codex
        for mod in (claude, codex):
            self.assertEqual(agents.check(mod, mod.DESCRIBE["id"]), [])
            self.assertTrue(all(c["run"] in agents.COMMAND_RUNS for c in mod.COMMANDS))
        runs = {c["name"]: c["run"] for c in claude.COMMANDS}
        self.assertEqual((runs["model"], runs["config"], runs["context"], runs["compact"]), ("menu", "terminal", "now", "now"))
        runs = {c["name"]: c["run"] for c in codex.COMMANDS}
        self.assertEqual((runs["model"], runs["review"], runs["status"], runs["compact"]), ("menu", "menu", "terminal", "now"))
        self.assertNotIn("approvals", runs)  # Codex 0.159.1: "Unrecognized command '/approvals'"; it is /permissions
        self.assertEqual({c["name"]: c["setting"] for c in claude.COMMANDS if c.get("setting")},
                         {"model": "model", "effort": "effort"})
        self.assertEqual({c["name"]: c["setting"] for c in codex.COMMANDS if c.get("setting")}, {"model": "model"})

    def test_a_wrong_run_is_a_problem_of_the_adapter(self):
        import types
        m = types.SimpleNamespace(DESCRIBE={"id": "x", "name": "X", "command": "x"},
                                  COMMANDS=[{"name": "go", "description": "", "hint": "", "run": "later"}])
        self.assertTrue(any("'later', not one of now, menu, terminal" in p for p in agents.check(m)))
        m.COMMANDS = [{"name": "go", "description": "", "hint": "", "setting": "colour"}]
        self.assertTrue(any("'colour', which is no setting of the Settings dialog" in p for p in agents.check(m)))


class ClaudeCommandLines(unittest.TestCase):
    """#5220: Claude Code writes a command it ran by itself, and what it printed, as system lines 'local_command'."""

    def test_the_command_is_a_turn_and_its_output_a_block(self):
        from river.agents import claude
        def line(content):
            return {"type": "system", "subtype": "local_command", "content": content, "timestamp": "2026-11-11T08:30:00Z"}
        got, turn = claude.parse_chat_line(line("<command-name>/context</command-name>\n  <command-message>context"
                                                "</command-message>\n  <command-args></command-args>"))
        self.assertEqual((got, turn), ([{"at": "2026-11-11T08:30:00Z", "role": "user", "text": "/context",
                                          "command": True}], None))
        got, _ = claude.parse_chat_line(line("<local-command-stdout> \x1b[1mContext Usage\x1b[22m\n"
                                             "\x1b[38;5;244m⛀ \x1b[39m34k/700k</local-command-stdout>"))
        self.assertEqual([(e["role"], e["text"]) for e in got], [("output", "Context Usage\n⛀ 34k/700k")])
        self.assertEqual(claude.parse_chat_line(line("<local-command-stdout></local-command-stdout>"))[0], [])
