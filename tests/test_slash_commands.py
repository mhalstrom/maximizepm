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
