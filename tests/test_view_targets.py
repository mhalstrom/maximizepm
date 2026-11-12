"""The Targets tab of the page (#4656): actions that cause a release event run its hooks."""
import contextlib
import io
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import core, server
from river.core import RiverError


class PageHooks(unittest.TestCase):
    """An action of the page that causes a release event: maxpm serve runs the hook after the answer."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()
        core.register(self.c, "mark", human=True)
        core.target_add(self.c, "web", "push")
        core.project_add(self.c, "site", target="web", path=self.dir.name)
        x = core.item_add(self.c, "site", "page", actor="mark")["id"]
        self.deploy = core.done(self.c, x, "commit", "mark", ship_it=True)["shipped_in"]
        self.log = os.path.join(self.dir.name, "hook.log")
        script = os.path.join(self.dir.name, "hook.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(f"import os\nopen({self.log!r}, 'a').write(os.environ['MAXPM_EVENT'] + ' ' + "
                    "os.environ['MAXPM_REV'] + '\\n')\n")
        self.cmd = f'"{sys.executable}" "{script}"'
        quiet = contextlib.redirect_stdout(io.StringIO())  # the thread of maxpm serve prints each result
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def tearDown(self):
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def post(self, op, **args):
        data = json.dumps({"op": op, "args": args, "actor": "mark"}).encode()
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.rfile, out = "/api/action", ("127.0.0.1", 5555), io.BytesIO(data), {}
        h.headers = {"Host": "127.0.0.1:8765", "Content-Length": str(len(data))}
        h._send = lambda code, body, ctype=None: out.update(code=code, body=body)
        h.do_POST()
        return out["code"], out["body"]

    def ended(self):
        import time
        for _ in range(200):
            rows = self.c.execute("SELECT ended_at FROM hook_runs").fetchall()
            if rows and all(r["ended_at"] for r in rows) and not server.HOOKS["running"]:
                return len(rows)
            time.sleep(0.05)
        self.fail("the hook did not run")

    def test_a_page_action_sets_a_hook_and_its_event_runs_it_in_a_thread(self):
        code, body = self.post("target_hook", target="web", event="release-cut", command=self.cmd)
        self.assertEqual((code, body["result"]["changed"]), (200, "set"))
        self.assertFalse(server.hooks_soon(self.c))  # nothing waits
        code, body = self.post("target_cut", target="web", rev="abc1234")
        self.assertEqual(code, 200)
        self.assertEqual(self.ended(), 1)
        with open(self.log, encoding="utf-8") as f:
            self.assertEqual(f.read(), "release-cut abc1234\n")
        self.assertTrue(any(e["change"].startswith("hook release-cut: exit 0, ")
                            for e in core.item_show(self.c, self.deploy)["events"]))

    @unittest.skipUnless(shutil.which("node"), "node is not on this machine")
    def test_the_targets_tab_shows_each_hook_with_its_last_result(self):
        """Node runs the page's own component (components/targetHooks.js) on the state of the queue (#1827)."""
        def html():
            comp = (Path(server.__file__).parent / "static" / "components" / "targetHooks.js").as_uri()
            st = core.state(self.c)
            code = (f"const {{ hooksHtml }} = await import({json.dumps(comp)});\n"
                    f"console.log(hooksHtml({json.dumps(st['targets'][0])}, {json.dumps(st['hook_events'])}));")
            r = subprocess.run(["node", "--input-type=module"], input=code, capture_output=True, encoding="utf-8", timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout
        self.assertEqual(list(core.state(self.c)["hook_events"]), list(core.HOOK_EVENTS))
        # No hook: the fold says none, its hover says what a hook is, and it offers each event.
        h = html()
        self.assertIn("<summary>Hooks: none</summary>", h)
        self.assertIn('title="Commands that run on release events"', h)
        for event in core.HOOK_EVENTS:
            self.assertIn(f'data-hook="{event}" data-target="web" title="Add a hook on {event}"', h)
        self.assertNotIn("data-clear", h)
        # A hook that failed and one that never ran; the other two events are free.
        bad = f'"{sys.executable}" -c "import sys; sys.exit(3) # <b>"'
        core.target_hook(self.c, "web", "release-cut", bad, actor="mark")
        core.target_hook(self.c, "web", "deployed", self.cmd, actor="mark")
        core.target_cut(self.c, "web", "abc1234", "mark")
        self.assertEqual([x["ok"] for x in core.run_hooks(self.c)], [False])
        h = html()
        cut, deployed, free = h.split('<div class="st hook t-s">')[1:3] + [h.split("add: ")[1]]
        self.assertIn('<summary>Hooks: release-cut, deployed · <span style="color:var(--warn)">release-cut failed</span></summary>', h)
        self.assertIn("<b>release-cut</b>", cut)
        self.assertIn('# &lt;b&gt;&quot;">', cut)  # the command (its hover) is text, never markup
        self.assertRegex(cut, rf'color:var\(--warn\)"><span class="link" data-open="{self.deploy}">'
                              rf'#{self.deploy}</span> exit 3, \d+s ago</span>')
        self.assertIn('data-hook="release-cut" data-target="web" data-clear="1"', cut)
        self.assertIn("<b>deployed</b>", deployed)
        self.assertIn('<span class="muted">never ran</span>', deployed)
        self.assertEqual(re.findall(r'data-hook="([a-z-]+)"', free), ["review-passed", "review-failed", "deploy"])
        # The hook passes on the next run: the line is not a warning any more.
        core.target_hook(self.c, "web", "release-cut", self.cmd, actor="mark")
        self.c.execute("UPDATE hook_runs SET exit_code=0")
        h = html()
        self.assertRegex(h, r'class="muted"><span class="link" data-open="\d+">#\d+</span> exit 0, ')
        self.assertNotIn(" failed</span></summary>", h)

    def test_the_loop_runs_a_hook_that_its_command_left_behind(self):
        core.target_hook(self.c, "web", "release-cut", self.cmd, actor="mark")
        core.target_cut(self.c, "web", "abc1234", "mark")  # a command that ends before it runs the hook
        self.assertFalse(server.hooks_soon(self.c))  # that command may still run it
        self.c.execute("UPDATE hook_runs SET queued_at='2000-01-01T00:00:00Z'")
        self.assertTrue(server.hooks_soon(self.c))
        self.assertEqual(self.ended(), 1)
        with open(self.log, encoding="utf-8") as f:
            self.assertEqual(f.read(), "release-cut abc1234\n")
