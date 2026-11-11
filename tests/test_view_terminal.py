"""The Terminal tab (#4803): plain shells on this computer in the page, for a person, through the relay only when
web_terminal_relay is on."""
import json
import os
import tempfile
import unittest

from river import core, relay, server
from river.routes import terminal


class FakeShellTmux:
    """Enough of tmux for the shells: one session of panes, with the options the route sets."""

    def __init__(self):
        self.panes, self.n, self.calls = [], 0, []

    def __call__(self, a):
        self.calls.append(a)
        if ";" in a:  # _pane_screen: display-message, then capture-pane
            i = a.index(";")
            head, cap = self(a[:i]), self(a[i + 1:])
            return None if head is None or cap is None else head + "\n" + cap
        cmd, opt = a[0], (lambda flag: a[a.index(flag) + 1] if flag in a else None)
        pane = next((p for p in self.panes if p["id"] == opt("-t")), None)
        if cmd == "has-session":
            return "" if self.panes else None
        if cmd in ("new-session", "new-window"):
            p = {"id": f"%{self.n}", "opts": {}, "keys": [], "cwd": opt("-c")}
            self.n += 1
            self.panes.append(p)
            return p["id"]
        if cmd == "set-option":
            pane["opts"][a[-2]] = a[-1]
            return ""
        if cmd == "list-panes":
            return "\n".join(f"{p['id']}|{p['opts'].get('@maxpm_name', '')}|{p['opts'].get('@maxpm_by', '')}|"
                             f"{p['opts'].get('@maxpm_started', '')}|-zsh|{p['cwd']}" for p in self.panes)
        if cmd == "send-keys":
            pane["keys"].append(a[-1])
            return ""
        if cmd == "kill-pane":
            self.panes.remove(pane)
            return ""
        if cmd == "display-message":
            return (f"80|24|2|0|zsh|{terminal.SHELL_SESSION}:0.0|/dev/ttys00{pane['id'][1:]}|"
                    f"{pane['opts'].get('@maxpm_name', '')}") if pane else None
        if cmd == "capture-pane":
            return "$ " + "".join(pane["keys"]) if pane else None
        return ""


class Shells(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()
        core.register(self.c, "mark", human=True)
        core.register(self.c, "ag")
        self.tmux = FakeShellTmux()
        old = server.TMUX_RUNNER
        server.TMUX_RUNNER = self.tmux
        self.addCleanup(setattr, server, "TMUX_RUNNER", old)

    def tearDown(self):
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def get(self, path, via_relay=False):
        h = server.Handler.__new__(server.Handler)
        headers = {"Host": "127.0.0.1:8765"}
        if via_relay:
            headers["X-Maxpm-Relay"] = relay.PAGE_SECRET
        h.path, h.client_address, h.headers, out = path, ("127.0.0.1", 5555), headers, {}
        h._send = lambda code, body, ctype=None, headers=None: out.update(code=code, body=json.loads(json.dumps(body)))
        h.do_GET()
        return out["code"], out["body"]

    def op(self, name, args, who, via_relay=False):
        tok = core.EVENT_VIA.set("relay" if via_relay else None)
        try:
            return server.OPS[name](self.c, args, who)
        finally:
            core.EVENT_VIA.reset(tok)

    def test_a_person_opens_types_reads_and_closes_a_shell(self):
        self.assertEqual(self.get("/api/shell")[1]["shells"], [])
        got = self.op("shell_open", {}, "mark")
        self.assertEqual(got["name"], "shell 1")
        self.assertIn("wrangler", got["note"], "the page says how a localhost sign-in finishes from afar")
        pane = got["pane"]
        self.assertEqual(self.tmux.panes[0]["cwd"], os.path.expanduser("~"))
        rows = self.get("/api/shell")[1]["shells"]
        self.assertEqual([(r["pane"], r["name"], r["by"], r["running"]) for r in rows], [(pane, "shell 1", "mark", "zsh")])
        self.op("shell_keys", {"pane": pane, "keys": [{"text": "echo hi"}, {"key": "Enter"}]}, "mark")
        code, screen = self.get(f"/api/shell?pane={pane}")
        self.assertEqual((code, screen["text"]), (200, "$ echo hiEnter"))
        self.assertEqual(self.op("shell_open", {}, "mark")["name"], "shell 2")
        self.op("shell_close", {"pane": pane}, "mark")
        self.assertEqual([r["name"] for r in self.get("/api/shell")[1]["shells"]], ["shell 2"])
        changes = [e["change"] for e in core.recent_events(self.c)]
        self.assertTrue(any(c.startswith("opened a terminal on this computer (shell 1") for c in changes))
        self.assertTrue(any(c.startswith("closed a terminal on this computer") for c in changes))

    def test_never_an_agent_nor_an_agents_pane_nor_a_bad_key(self):
        with self.assertRaisesRegex(core.RiverError, "only a person"):
            self.op("shell_open", {}, "ag")
        with self.assertRaisesRegex(core.RiverError, "only a person"):
            self.op("shell_open", {}, None)
        pane = self.op("shell_open", {}, "mark")["pane"]
        with self.assertRaisesRegex(core.RiverError, "no open terminal %99"):
            self.op("shell_keys", {"pane": "%99", "keys": [{"text": "x"}]}, "mark")
        self.assertEqual(self.get("/api/shell?pane=%2599")[0], 404, "an agent's pane is not a shell")
        with self.assertRaisesRegex(core.RiverError, "not a key MaximizePM sends"):
            self.op("shell_keys", {"pane": pane, "keys": [{"key": "F99"}]}, "mark")
        self.assertIn("shell_keys", server.TYPING_OPS, "the keys need the page's token")
        self.assertIn("shell_open", server.TYPING_OPS)

    def test_the_settings_and_the_relay(self):
        # Through the relay: off until web_terminal_relay is on.
        code, body = self.get("/api/shell", via_relay=True)
        self.assertEqual((code, body["via_relay"], body["web_terminal"]), (403, True, True))
        with self.assertRaisesRegex(core.RiverError, "off through the relay"):
            self.op("shell_open", {}, "mark", via_relay=True)
        core.config_set(self.c, "web_terminal_relay", "on")
        self.assertEqual(self.get("/api/shell", via_relay=True)[0], 200)
        self.op("shell_open", {}, "mark", via_relay=True)
        self.assertTrue(any(e["actor"] == "mark@relay" and e["change"].startswith("opened a terminal")
                            for e in core.recent_events(self.c)), "it shows that it came through the relay")
        # Off on this computer: off everywhere.
        core.config_set(self.c, "web_terminal", "off")
        self.assertEqual(self.get("/api/shell")[0], 403)
        with self.assertRaisesRegex(core.RiverError, "off on this computer"):
            self.op("shell_open", {}, "mark")
        with self.assertRaisesRegex(core.RiverError, "on or off"):
            core.config_set(self.c, "web_terminal_relay", "yes")

    def test_the_page_has_the_tab(self):
        page = server.page_html("/terminal").decode()
        self.assertIn('data-tab="terminal"', page)
        self.assertIn('id="tab-terminal"', page)
        self.assertIn("terminal", server.PAGE_VIEWS)


if __name__ == "__main__":
    unittest.main()
