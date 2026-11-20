"""maxpm agent-login claude|codex (#5877): an agent CLI's sign-in in a detached tmux window, its link and code read
from the window's screen, and Claude Code's code sent back. A fake tmux plays the window."""

import contextlib
import io
import unittest

from river import agentlogin, cli, core, server

CLAUDE_SCREEN = ("Browser didn't open? Use the url below to sign in (c to copy)\n\n"
                 "https://claude.ai/oauth/authorize?code=true&client_id=abc&state=xyz\n\n"
                 "Paste code here if prompted >")
CODEX_SCREEN = ("Follow these steps to sign in with ChatGPT using device code authorization:\n"
                "1. Open this link in your browser and sign in to your account\n"
                "   https://auth.openai.com/codex/device\n"
                "2. Enter this one-time code (expires in 15 minutes)\n   ABCD-12345\n")


class FakeTmux:
    def __init__(self, screen):
        self.screen = screen
        self.calls = []
        self.session = False
        self.window = False

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "has-session":
            return "" if self.session else None
        if args[0] in ("new-session", "new-window"):
            self.session = self.window = True
            return ""
        if args[0] == "kill-window":
            self.window = False
            return ""
        if args[0] == "list-panes":
            return "%1" if self.window else None
        if args[0] == "capture-pane":
            return self.screen if self.window else None
        if args[0] == "send-keys":
            return ""
        raise AssertionError(args)


class AgentLogin(unittest.TestCase):
    def setUp(self):
        self.signed = {"claude": False, "codex": False, "agy": False}
        agentlogin.STATUS_RUNNER = lambda argv: 0 if self.signed[argv[0]] else 1
        self.addCleanup(setattr, agentlogin, "STATUS_RUNNER", None)
        old = (agentlogin.WAIT, agentlogin.DONE_WAIT, agentlogin.POLL)
        agentlogin.WAIT, agentlogin.DONE_WAIT, agentlogin.POLL = 0.3, 0.3, 0.01
        self.addCleanup(lambda: setattr(agentlogin, "WAIT", old[0]) or setattr(agentlogin, "DONE_WAIT", old[1])
                        or setattr(agentlogin, "POLL", old[2]))
        self.addCleanup(setattr, server, "TMUX_RUNNER", None)

    def tmux(self, screen):
        server.TMUX_RUNNER = FakeTmux(screen)
        return server.TMUX_RUNNER

    def run_cli(self, *words):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.run(list(words))
        return out.getvalue()

    def test_claude_shows_the_link_and_takes_the_code_back(self):
        t = self.tmux(CLAUDE_SCREEN)
        text = self.run_cli("agent-login", "claude")
        self.assertIn(["new-session", "-d", "-s", "maxpm", "-n", "signin-claude", "-x", "200", "-y", "50",
                       "claude auth login"], t.calls)
        self.assertIn("https://claude.ai/oauth/authorize?code=true&client_id=abc&state=xyz", text)
        self.assertIn("maxpm agent-login claude --code <code>", text)
        self.signed["claude"] = True  # the CLI took the code
        self.assertIn("Claude Code is signed in here.", self.run_cli("agent-login", "claude", "--code", "xY_z-9"))
        self.assertIn(["send-keys", "-t", "=maxpm:signin-claude", "-l", "xY_z-9"], t.calls)
        self.assertIn(["send-keys", "-t", "=maxpm:signin-claude", "Enter"], t.calls)

    def test_codex_shows_the_link_and_the_code_to_type(self):
        t = self.tmux(CODEX_SCREEN)
        t.session = True
        res = agentlogin.start("codex")
        self.assertEqual((res["url"], res["code"]), ("https://auth.openai.com/codex/device", "ABCD-12345"))
        self.assertIn(["new-window", "-d", "-t", "=maxpm:", "-n", "signin-codex", "codex login --device-auth"], t.calls)
        self.assertIn(["kill-window", "-t", "=maxpm:signin-codex"], t.calls)  # an older one goes first
        with self.assertRaisesRegex(core.RiverError, "takes no code here"):
            agentlogin.paste("codex", "ABCD-12345")

    def test_agy_takes_the_first_login_method_then_shows_the_link_and_takes_the_code_back(self):
        """#6248: agy asks for the login method first (the manager saw 'Select login method: 1. Google OAuth 2. Use a
        Google Cloud project' on 2026-11-19); Enter takes Google OAuth, then the link shows. The link's screen here is
        made up: no host without a browser was at hand."""
        t = self.tmux("Select login method:\n> 1. Google OAuth\n  2. Use a Google Cloud project\n")
        sent = []

        def keys(args, t=t, call=t.__call__):
            if args[0] == "send-keys" and args[-1] == "Enter" and not sent:
                sent.append(args)
                t.screen = ("Open this link to sign in:\nhttps://accounts.google.com/o/oauth2/auth?client_id=a&state=b\n"
                            "Enter the authorization code:")
            return call(args)
        server.TMUX_RUNNER = keys
        res = agentlogin.start("agy")
        self.assertEqual(sent, [["send-keys", "-t", "=maxpm:signin-agy", "Enter"]])
        self.assertEqual((res["url"], res["paste"]), ("https://accounts.google.com/o/oauth2/auth?client_id=a&state=b", True))
        self.assertIn(["new-session", "-d", "-s", "maxpm", "-n", "signin-agy", "-x", "200", "-y", "50", "agy"], t.calls)
        self.signed["agy"] = True
        self.assertIn("Antigravity CLI is signed in here.", self.run_cli("agent-login", "agy", "--code", "4/0Ab-x"))
        self.assertIn(["send-keys", "-t", "=maxpm:signin-agy", "-l", "4/0Ab-x"], t.calls)

    def test_a_signed_in_cli_starts_nothing(self):
        t = self.tmux("")
        self.signed["codex"] = True
        self.assertIn("Codex is signed in here.", self.run_cli("agent-login", "codex"))
        self.assertEqual(t.calls, [])

    def test_no_link_names_the_window(self):
        self.tmux("Error: something else")
        with self.assertRaisesRegex(core.RiverError, "showed no sign-in link.*something else.*window signin-claude"):
            agentlogin.start("claude")

    def test_a_code_with_no_waiting_sign_in_is_refused(self):
        self.tmux("")
        with self.assertRaisesRegex(core.RiverError, "no Claude Code sign-in waits"):
            agentlogin.paste("claude", "abc")

    def test_a_code_the_cli_does_not_take(self):
        t = self.tmux(CLAUDE_SCREEN)
        agentlogin.start("claude")
        t.screen = "OAuth error: invalid code"
        with self.assertRaisesRegex(core.RiverError, "not signed in .*invalid code"):
            agentlogin.paste("claude", "wrong")

    def test_a_long_link_has_no_qr_code(self):
        text = agentlogin.render({"agent": "claude", "signed_in": False, "url": "https://x/" + "a" * 400,
                                  "paste": True, "next": "give the code"})
        self.assertIn("https://x/aaa", text)
        self.assertNotIn("▀", text)


if __name__ == "__main__":
    unittest.main()
