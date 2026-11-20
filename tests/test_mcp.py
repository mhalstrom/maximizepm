import io
import json
import os
import sys
import tempfile
import unittest

from river import core, mcp


class Mcp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        os.environ.pop("MAXPM_AGENT", None)
        c = core.connect()
        core.project_add(c, "demo", path=self.dir.name)
        core.item_add(c, "demo", "first job", context="start here")
        c.close()

    def tearDown(self):
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def talk(self, *msgs):
        out = io.StringIO()
        mcp.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
        return [json.loads(x) for x in out.getvalue().splitlines()]

    def test_initialize_list_and_work_loop(self):
        call = lambda i, name, args: {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}}
        r = self.talk({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                      {"jsonrpc": "2.0", "method": "notifications/initialized"},
                      {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                      call(3, "go", {"cwd": self.dir.name}),
                      call(4, "done", {"id": 1, "output": "did it"}),
                      call(5, "maxpm", {"args": ["show", "1"]}),
                      call(6, "maxpm", {"args": ["claim", "99"]}),
                      call(7, "maxpm", {"args": ["--nonsense"]}))
        self.assertEqual([x["id"] for x in r], [1, 2, 3, 4, 5, 6, 7])  # no reply to the notification
        self.assertEqual(r[0]["result"]["capabilities"], {"tools": {}})
        self.assertIn("go", [t["name"] for t in r[1]["result"]["tools"]])
        go = r[2]["result"]["content"][0]["text"]
        self.assertIn("YOUR ITEM #1: first job", go)
        self.assertFalse(r[3]["result"]["isError"])  # done ran as the agent go named
        self.assertIn("did it", r[4]["result"]["content"][0]["text"])
        self.assertTrue(r[5]["result"]["isError"])
        self.assertIn("no item 99", r[5]["result"]["content"][0]["text"])
        self.assertTrue(r[6]["result"]["isError"])

    def test_goal_tool(self):
        c = core.connect()
        core.goal_add(c, "demo", "ship", "it ships", "users install it")
        core.register(c, "ag")
        c.close()
        call = lambda i, args: {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                "params": {"name": "goal", "arguments": dict(args, **{"as": "ag"})}}
        r = self.talk(call(1, {"action": "list"}), call(2, {"action": "own", "name": "ship"}),
                      call(3, {"action": "show", "name": "ship"}), call(4, {"action": "done", "name": "ship", "result": "shipped"}),
                      call(5, {"action": "own"}))
        text = [x["result"]["content"][0]["text"] for x in r]
        self.assertIn("ship", text[0])
        self.assertIn("ag", text[2])
        self.assertFalse(r[3]["result"]["isError"])
        self.assertTrue(r[4]["result"]["isError"])
        c = core.connect()
        self.assertEqual(core.goal_show(c, "ship")["status"], "complete")
        c.close()

    def test_browser_tool_builds_the_words_of_the_command(self):
        """#5678: one tool browser with an action field; the flags, then -- and the positional words."""
        tool = next(t for t in mcp.TOOLS if t["name"] == "browser")
        self.assertIn("click", tool["inputSchema"]["properties"]["action"]["enum"])
        self.assertEqual(mcp.browser_argv({"action": "click", "target": "Edit", "in": "Invoice 1042", "json": True}),
                         ["--json", "browser", "click", "--in", "Invoice 1042", "--", "Edit"])
        self.assertEqual(mcp.browser_argv({"action": "fill", "target": "Amount", "text": "-5"}),
                         ["browser", "fill", "--", "Amount", "-5"])
        self.assertEqual(mcp.browser_argv({"action": "click", "x": 10, "y": 20}), ["browser", "click", "--", "10", "20"])
        self.assertEqual(mcp.browser_argv({"action": "dialog", "accept": False}), ["browser", "dialog", "--", "dismiss"])
        self.assertEqual(mcp.browser_argv({"action": "upload", "target": "Resume", "values": ["/p/cv.pdf"]}),
                         ["browser", "upload", "--", "Resume", "/p/cv.pdf"])  # #5679
        self.assertEqual(mcp.browser_argv({"action": "run", "text": "open x\nclick Go", "values": ["q=rust"]}),
                         ["browser", "run", "--lines", "open x\nclick Go", "--", "q=rust"])  # #5680
        self.assertEqual(mcp.browser_argv({"action": "click", "target": "Row", "double": True, "with": "shift"}),
                         ["browser", "click", "--with", "shift", "--double", "--", "Row"])  # #5669
        self.assertEqual(mcp.browser_argv({"action": "scroll", "amount": "-300"}), ["browser", "scroll", "--", "-300"])
        self.assertEqual(mcp.browser_argv({"action": "drag", "target": "Card", "to": "Done"}),
                         ["browser", "drag", "--", "Card", "Done"])
        self.assertEqual(mcp.browser_argv({"action": "extract", "rows": "tr", "fields": ["t=a"], "limit": 2}),
                         ["browser", "extract", "--rows", "tr", "--limit", "2", "--", "t=a"])
        self.assertEqual(mcp.browser_argv({"action": "snapshot", "all": True, "tab": "T1"}),
                         ["browser", "snapshot", "--tab", "T1", "--all"])
        with self.assertRaisesRegex(core.RiverError, "browser open needs the address"):
            mcp.browser_argv({"action": "open"})
        with self.assertRaisesRegex(core.RiverError, "browser action is one of"):
            mcp.browser_argv({"action": "fly"})

    def test_canvas_tool_has_the_operations_of_the_command(self):
        """#4648: put with the text itself (also text that starts with a dash), show, comments, reply, resolve."""
        c = core.connect()
        core.register(c, "ag")
        core.register(c, "mark", human=True)
        core.claim(c, 1, "ag")
        c.close()
        call = lambda i, args, who="ag": {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                          "params": {"name": "canvas", "arguments": dict(args, **{"as": who})}}
        put = self.talk(call(1, {"action": "put", "name": "notes", "kind": "md", "text": "---\ntitle: x\n---\n# Notes",
                                 "title": "My notes", "note": "-first"}))
        c = core.connect()
        core.canvas_comment(c, "notes", "-shorter", "mark", {"quote": "Notes"}, send_now=True)  # the person, on the page
        c.close()
        r = put + self.talk(call(4, {"action": "comments", "name": "notes", "open": True}),
                            call(5, {"action": "reply", "comment": "c1", "text": "done in v2"}),
                            call(6, {"action": "resolve", "comment": "c1", "note": "v2"}),
                            call(7, {"action": "list"}),
                            call(8, {"action": "show"}),
                            call(9, {"action": "fly", "name": "notes"}))
        text = [x["result"]["content"][0]["text"] for x in r]
        self.assertEqual([x["result"]["isError"] for x in r], [False] * 5 + [True, True])
        self.assertIn('notes  v1 md  demo  #1  "My notes"', text[0])
        self.assertIn('c1 [read] mark on "Notes" (v1): -shorter', text[1])
        self.assertIn("needs the canvas name", text[5])
        c = core.connect()
        d = core.canvas_show(c, "notes")
        c.close()
        self.assertEqual(d["shown"]["body"], "---\ntitle: x\n---\n# Notes")
        self.assertEqual((d["shown"]["note"], d["comments"][0]["state"]), ("-first", "resolved"))


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class ChatApp(unittest.TestCase):
    """A chat app with no folder (Claude desktop through maxpm mcp): setup, go, plan, and manage."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        os.environ.pop("MAXPM_AGENT", None)
        os.environ["MAXPM_CHAT"] = "1"
        c = core.connect()
        core.project_add(c, "site", path=os.path.join(self.dir.name, "site"))
        core.project_add(c, "book")
        self.code = core.item_add(c, "site", "fix the header", priority=0, touches="src/header.js")["id"]
        self.test = core.item_add(c, "site", "speed up tests", priority=0, check="make test")["id"]
        self.text = core.item_add(c, "book", "draft chapter one", priority=2)["id"]
        c.close()
        self.old = os.getcwd()
        os.chdir("/")  # the app starts its servers with no project folder

    def tearDown(self):
        os.chdir(self.old)
        for k in ("MAXPM_DB", "MAXPM_CHAT"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def call(self, srv, name, args=None):
        text, bad = srv.call(name, args or {})
        self.assertFalse(bad, text)
        return text

    def test_go_takes_only_work_that_needs_no_folder(self):
        srv = mcp.Server()
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertIn("no folder, no shell", r["result"]["instructions"])
        tools = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertIn("manage", {t["name"] for t in tools})
        self.assertNotIn("plan", {t["name"] for t in tools})  # no planner role (#4978)
        go = self.call(srv, "go")
        self.assertIn(f"YOUR ITEM #{self.text}: draft chapter one", go)
        self.assertRegex(srv.agent, r"^chat-")
        self.assertIn("Skipped in this chat", go)
        self.assertIn(f"#{self.code} fix the header: needs a folder", go)
        self.assertIn("IN A CHAT (no folder, no shell)", go)
        self.assertIn("edit <id> --notes", go)
        self.call(srv, "done", {"id": self.text, "output": "Chapter one: the river at dawn ..."})
        go = self.call(srv, "go")
        self.assertIn("NOTHING FOR THIS CHAT NOW", go)
        self.assertNotIn("maxpm wait", go)
        # The code items stay for coding agents.
        c = core.connect()
        self.assertEqual({core.item_show(c, i)["status"] for i in (self.code, self.test)}, {"open"})
        c.close()

    def test_manage_in_a_chat(self):
        srv = mcp.Server()
        m = self.call(srv, "manage")
        self.assertRegex(srv.agent, r"^manager-")
        self.assertIn("manage --watch is for a terminal", m)
        self.call(srv, "maxpm", {"args": ["needs-you"]})  # runs as the manager go named

    def test_setup_merges_into_the_claude_desktop_config(self):
        from river import agents  # the MCP config writers are in the adapters' setup part (#5184)
        f = os.path.join(self.dir.name, "Claude", "claude_desktop_config.json")
        os.makedirs(os.path.dirname(f))
        with open(f, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}, fh)
        self.assertIn("added MaximizePM", agents.get("claude").write_mcp_config(f))
        data = load(f)
        self.assertEqual(data["mcpServers"]["other"], {"command": "x"})
        self.assertEqual(data["theme"], "dark")
        river = data["mcpServers"]["maxpm"]
        self.assertEqual(river["args"][-1], "mcp")
        self.assertTrue(os.path.isabs(river["command"]))
        self.assertEqual(river["env"], {"MAXPM_DB": str(core.db_path().resolve()), "MAXPM_CHAT": "1"})
        self.assertTrue(os.path.exists(f + ".bak"))
        self.assertIn("nothing changed", agents.get("claude").write_mcp_config(f))
        self.assertIn("took MaximizePM out", agents.get("claude").write_mcp_config(f, remove=True))
        self.assertEqual(load(f)["mcpServers"], {"other": {"command": "x"}})
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        with self.assertRaisesRegex(core.RiverError, "not valid JSON"):
            agents.get("claude").write_mcp_config(f)
        new = os.path.join(self.dir.name, "fresh", "claude_desktop_config.json")
        agents.get("claude").write_mcp_config(new)
        self.assertEqual(list(load(new)["mcpServers"]), ["maxpm"])

    @unittest.skipUnless(sys.version_info >= (3, 11), "tomllib is new in Python 3.11")
    def test_setup_merges_into_the_codex_config_that_chatgpt_shares(self):
        from river import agents
        f = os.path.join(self.dir.name, ".codex", "config.toml")
        os.makedirs(os.path.dirname(f))
        old = ('model = "sol"\n\n[mcp_servers.node_repl]\ncommand = "/x/node"\nargs = []\n\n'
               '[mcp_servers.node_repl.env]\nA = "1"\n\n[apps.x.tools."y.z"]\napproval_mode = "approve"\n')
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(old)
        self.assertIn("Work or Codex mode", agents.get("codex").write_mcp_config(f))
        with open(f, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith(old.rstrip("\n")))  # every other line stays as it was
        import tomllib
        data = tomllib.loads(text)
        self.assertEqual(data["mcp_servers"]["node_repl"]["env"], {"A": "1"})
        self.assertEqual(data["mcp_servers"]["maxpm"]["env"], {"MAXPM_DB": str(core.db_path().resolve()), "MAXPM_CHAT": "1"})
        self.assertEqual(data["mcp_servers"]["maxpm"]["args"][-1], "mcp")
        self.assertIn("nothing changed", agents.get("codex").write_mcp_config(f))
        # A changed river entry is replaced, not doubled.
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(text.replace('"MAXPM_CHAT" = "1"', "").replace("MAXPM_CHAT = \"1\"", 'MAXPM_CHAT = "0"'))
        agents.get("codex").write_mcp_config(f)
        with open(f, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text.count("[mcp_servers.maxpm]"), 1)
        self.assertIn('MAXPM_CHAT = "1"', text)
        self.assertIn("took MaximizePM out", agents.get("codex").write_mcp_config(f, remove=True))
        with open(f, encoding="utf-8") as fh:
            self.assertEqual(fh.read().rstrip("\n"), old.rstrip("\n"))
        self.assertIn("nothing changed", agents.get("codex").write_mcp_config(f, remove=True))
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("[broken")
        with self.assertRaisesRegex(core.RiverError, "not valid TOML"):
            agents.get("codex").write_mcp_config(f)

    def test_a_session_in_a_project_folder_is_not_a_chat(self):
        # The Codex CLI reads the same config: its maxpm mcp runs with MAXPM_CHAT=1 in the project folder.
        os.makedirs(os.path.join(self.dir.name, "site"))
        srv = mcp.Server()
        go = self.call(srv, "go", {"cwd": os.path.join(self.dir.name, "site")})
        self.assertIn(f"YOUR ITEM #{self.code}: fix the header", go)
        self.assertNotIn("IN A CHAT", go)


class HttpEndpoint(unittest.TestCase):
    """/mcp in maxpm serve: the same tools over Streamable HTTP, for this computer only until it has sign-in."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        os.environ.pop("MAXPM_AGENT", None)
        os.environ.pop("MAXPM_CHAT", None)
        os.environ["MAXPM_AGENT"] = "whoever-started-serve"
        self.addCleanup(os.environ.pop, "MAXPM_AGENT", None)
        c = core.connect()
        core.project_add(c, "site", path=self.dir.name)
        self.code = core.item_add(c, "site", "fix the header", priority=0, touches="src/header.js")["id"]
        self.text = core.item_add(c, "site", "write the launch post")["id"]
        c.close()
        self.old = os.getcwd()
        os.chdir(self.dir.name)  # maxpm serve started in a project folder: a web chat is still in none

    def tearDown(self):
        os.chdir(self.old)
        os.environ.pop("MAXPM_DB", None)
        mcp.HTTP_SESSIONS.clear()
        self.dir.cleanup()

    def request(self, method, body=None, headers=None, client="127.0.0.1"):
        from river import server
        h = server.Handler.__new__(server.Handler)
        data = json.dumps(body).encode() if body is not None else b""
        hdrs = {"Host": "127.0.0.1:8765", "Content-Length": str(len(data)), **(headers or {})}
        import email.message
        msg = email.message.Message()
        for k, v in hdrs.items():
            msg[k] = v
        h.headers, h.path, h.client_address, h.rfile, h.wfile = msg, "/mcp", (client, 5000), io.BytesIO(data), io.BytesIO()
        out = {"headers": {}}
        h.send_response = lambda code, msg=None: out.update(code=code)
        h.send_header = lambda k, v: out["headers"].update({k: v})
        h.end_headers = lambda: None
        getattr(h, "do_" + method)()
        raw = h.wfile.getvalue()
        return out["code"], (json.loads(raw) if raw else None), out["headers"]

    def test_a_session_over_http(self):
        code, r, hd = self.request("POST", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(code, 200)
        self.assertIn("no folder, no shell", r["result"]["instructions"])
        sid = {"Mcp-Session-Id": hd["Mcp-Session-Id"]}
        code, r, _ = self.request("POST", {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        self.assertEqual((code, r), (202, None))
        code, r, _ = self.request("POST", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                           "params": {"name": "go", "arguments": {}}}, sid)
        text = r["result"]["content"][0]["text"]
        self.assertIn(f"YOUR ITEM #{self.text}: write the launch post", text)  # a chat: the code item waits
        self.assertIn("IN A CHAT", text)
        self.assertRegex(text, r"You are MaximizePM agent chat-")
        code, r, _ = self.request("POST", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                           "params": {"name": "done", "arguments": {"id": self.text, "output": "posted"}}}, sid)
        self.assertFalse(r["result"]["isError"])  # as the agent go named in this session
        _, r, _ = self.request("POST", {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                        "params": {"name": "go", "arguments": {"cwd": "/tmp"}}}, sid)
        self.assertIn("no folder on this computer", r["result"]["content"][0]["text"])
        self.assertEqual(self.request("GET", headers=sid)[0], 405)
        self.assertEqual(self.request("DELETE", headers=sid)[0], 200)
        self.assertEqual(self.request("POST", {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}, sid)[0], 404)
        self.assertEqual(self.request("POST", {"jsonrpc": "2.0", "id": 6, "method": "tools/list"})[0], 400)

    def test_only_this_computer_straight_to_river_serve(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        for headers, client, why in (
                ({"CF-Connecting-IP": "203.0.113.9"}, "127.0.0.1", "tunnel or proxy"),  # cloudflared connects locally
                ({"X-Forwarded-For": "203.0.113.9"}, "127.0.0.1", "tunnel or proxy"),
                ({"Host": "river.example.com"}, "127.0.0.1", "only on 127.0.0.1"),
                ({"Origin": "https://evil.example"}, "127.0.0.1", "cross-origin"),
                ({}, "192.168.1.20", "only this computer")):
            code, r, _ = self.request("POST", init, headers, client)
            self.assertEqual(code, 403)
            self.assertIn(why, r["error"])
        self.assertEqual(self.request("POST", init, {"Host": "localhost:8765", "Origin": "http://localhost:8765"})[0], 200)
        self.assertEqual(mcp._host_only("[::1]:8765"), "::1")


class Planner(unittest.TestCase):
    """#6190: /mcp/plan and maxpm mcp --plan: a chat app plans with the person (projects, goals, tasks, links), as the
    person, with no tool that takes, finishes or launches work."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.addCleanup(os.environ.pop, "MAXPM_DB", None)
        # A guard, not only an env line: these tests add projects and tasks as the person, so they never run on a
        # queue outside a temporary folder (a #6190 run by hand once wrote to the real queue).
        tmp = os.path.realpath(tempfile.gettempdir())
        if not str(core.db_path().resolve()).startswith(tmp + os.sep):
            raise RuntimeError(f"refused: the planner tests run only on a queue under {tmp}, not {core.db_path()}")
        for k in ("MAXPM_AGENT", "MAXPM_CHAT", core.TOKEN_ENV):
            old = os.environ.pop(k, None)
            if old is not None:
                self.addCleanup(os.environ.__setitem__, k, old)
        self.addCleanup(mcp.HTTP_SESSIONS.clear)
        c = core.connect()
        core.register(c, "mark", human=True)
        c.close()

    def request(self, method, path, body=None, headers=None):
        from river import server
        import email.message
        h = server.Handler.__new__(server.Handler)
        data = json.dumps(body).encode() if body is not None else b""
        msg = email.message.Message()
        for k, v in {"Host": "127.0.0.1:8765", "Content-Length": str(len(data)), **(headers or {})}.items():
            msg[k] = v
        h.headers, h.path, h.client_address = msg, path, ("127.0.0.1", 5000)
        h.rfile, h.wfile = io.BytesIO(data), io.BytesIO()
        out = {"headers": {}}
        h.send_response = lambda code, m=None: out.update(code=code)
        h.send_header = lambda k, v: out["headers"].update({k: v})
        h.end_headers = lambda: None
        getattr(h, "do_" + method)()
        raw = h.wfile.getvalue()
        return out["code"], (json.loads(raw) if raw else None), out["headers"]

    def session(self, path="/mcp/plan", client="claude-ai"):
        code, r, hd = self.request("POST", path, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                  "params": {"clientInfo": {"name": client, "version": "1"}}})
        self.assertEqual(code, 200)
        sid = {"Mcp-Session-Id": hd["Mcp-Session-Id"]}
        n = iter(range(2, 1000))

        def call(_tool, **args):
            _, r, _ = self.request("POST", path, {"jsonrpc": "2.0", "id": next(n), "method": "tools/call",
                                                  "params": {"name": _tool, "arguments": args}}, sid)
            return r["result"]["content"][0]["text"], r["result"]["isError"]
        return r["result"], sid, call

    def test_plan_a_project_over_http(self):
        init, sid, call = self.session()
        self.assertEqual(init["serverInfo"]["name"], "maximizepm-planner")
        _, r, _ = self.request("POST", "/mcp/plan", {"jsonrpc": "2.0", "id": 9, "method": "tools/list"}, sid)
        names = {t["name"] for t in r["result"]["tools"]}
        self.assertEqual(names, mcp.PLAN_NAMES)
        self.assertFalse(names & {"maxpm", "go", "done", "claim", "manage", "browser", "canvas", "inbox"})
        text, bad = call("add_project", name="shop", description="The online shop")
        self.assertFalse(bad, text)
        text, bad = call("add_goal", project="shop", name="checkout", outcome="People pay in one step",
                         done_when="a test order passes")
        self.assertFalse(bad, text)
        text, bad = call("add_task", project="shop", title="Choose the payment provider", doer="human",
                         short="Choose the provider")
        self.assertFalse(bad, text)
        c = core.connect()
        first = c.execute("SELECT id FROM items WHERE title='Choose the payment provider'").fetchone()["id"]
        text, bad = call("add_task", project="shop", title="Build the payment form", goal=["checkout"], priority=1,
                         waits_on=[first], context="Use the provider that the person chooses")
        self.assertFalse(bad, text)
        second = c.execute("SELECT id FROM items WHERE title='Build the payment form'").fetchone()
        self.assertIsNotNone(second)
        second = second["id"]
        self.assertTrue(c.execute("SELECT 1 FROM deps WHERE item_id=? AND blocked_by=?", (second, first)).fetchone())
        self.assertEqual(c.execute("SELECT doer FROM items WHERE id=?", (first,)).fetchone()["doer"], "human")
        # The person added them, through Claude: the history says mark@claude-ai.
        actors = {r["actor"] for r in c.execute("SELECT actor FROM events WHERE item_id IN (?,?) AND actor<>'maxpm'",
                                                (first, second))}
        self.assertEqual(actors, {"mark@claude-ai"})  # MaximizePM's own entries (needs you) stay 'maxpm'
        text, bad = call("show_task", id=second)
        self.assertFalse(bad, text)
        self.assertIn("Build the payment form", text)
        self.assertIn(f"#{first}", text)
        self.assertNotIn("[mark:", text)  # the person's own footer is not this chat's
        text, _ = call("list_tasks", project="shop")
        self.assertIn("Choose the payment provider", text)
        text, _ = call("show_goal", name="checkout")
        self.assertIn("Build the payment form", text)
        text, bad = call("edit_task", id=second, priority=0, short="Payment form")
        self.assertFalse(bad, text)
        self.assertEqual(tuple(c.execute("SELECT priority, short FROM items WHERE id=?", (second,)).fetchone()),
                         (0, "Payment form"))
        self.assertFalse(call("unlink_tasks", id=second, waits_on=[first])[1])
        self.assertFalse(call("link_tasks", id=second, waits_on=[first])[1])
        self.assertFalse(call("backlog_task", id=second, reason="after the provider")[1])
        self.assertFalse(call("activate_task", id=second)[1])  # a person's act: a session may not activate
        self.assertFalse(call("drop_task", id=first, reason="the shop takes cash")[1])
        self.assertEqual(c.execute("SELECT status FROM items WHERE id=?", (first,)).fetchone()["status"], "dropped")
        self.assertIn("## Writing tasks", call("planning_guide")[0])
        c.close()

    def test_design_doc_over_http(self):
        """#6221: the planner keeps a design document as an expert does: it starts it for a goal or a task (it opens in
        the page), writes its parts, reads it, its comments and answered decisions, and links its plan to tasks."""
        from river import cli, server
        opened = []
        real_ask = cli.ask_server
        cli.ask_server = lambda conn, op, args, actor=None, **k: opened.append((op, args, actor)) or {"opened": True}
        self.addCleanup(setattr, cli, "ask_server", real_ask)
        _, sid, call = self.session()
        self.assertFalse(call("add_project", name="shop", description="The online shop")[1])
        self.assertFalse(call("add_goal", project="shop", name="checkout", outcome="People pay in one step")[1])
        self.assertFalse(call("add_task", project="shop", title="Build the payment form", goal=["checkout"])[1])
        c = core.connect()
        self.addCleanup(c.close)
        task = c.execute("SELECT id FROM items WHERE title='Build the payment form'").fetchone()["id"]
        core.goal_handoff(c, "checkout", "The provider is Stripe; the form is left", "mark")
        self.assertIn("needs the task (task) or the goal", call("start_doc", title="x")[0])
        text, bad = call("start_doc", goal="checkout", title="Checkout")
        self.assertFalse(bad, text)
        self.assertIn("goal-checkout", text)
        self.assertIn("opened in the side panel", text)
        doc = core.canvas_show(c, "goal-checkout")
        self.assertEqual(doc["project"], "shop")
        self.assertEqual([v["author"] for v in doc["versions"]], ["mark@claude-ai"])  # the app wrote it, as mark
        self.assertEqual(opened, [("canvas_open", {"name": str(doc["id"])}, "mark")])
        decision = ("Decision 1 of 1: Provider\nWhat you decide: which provider takes the payments (A or B).\n"
                    "My recommendation: A.\nYour answer: \"A\"")
        text, bad = call("write_section", doc="goal-checkout", heading="Decisions", text=decision)
        self.assertFalse(bad, text)
        text, bad = call("write_section", doc="goal-checkout", heading="Plan", text=f"### The form\n\n- #{task}")
        self.assertFalse(bad, text)
        # The person comments in the page; the planner reads it, answers, and records an answer from the chat.
        com = core.canvas_comment(c, "goal-checkout", "Say why A", "mark", send_now=True)
        text, bad = call("doc_comments", doc="goal-checkout", open=True)
        self.assertFalse(bad, text)
        self.assertIn("Say why A", text)
        self.assertFalse(call("reply_comment", comment=f"c{com['id']}", text="A has the lower fee")[1])
        self.assertFalse(call("reply_comment", comment=f"c{com['id']}", resolve=True, text="v4 says why")[1])
        self.assertEqual(core._canvas_comment(c, com["id"])["state"], "resolved")
        text, bad = call("answer_decision", doc="goal-checkout", decision=1, answer="A")
        self.assertFalse(bad, text)
        self.assertIn("Answer to decision 1 of 1 (Provider): A", call("doc_comments", doc="goal-checkout")[0])
        text, bad = call("read_doc", doc="goal-checkout")
        self.assertFalse(bad, text)
        self.assertIn("Decision 1 of 1: Provider", text)
        text, bad = call("read_doc", doc="goal-checkout", plan=True)
        self.assertFalse(bad, text)
        self.assertIn(f"#{task}", text)
        self.assertEqual({v["author"] for v in core.canvas_show(c, "goal-checkout")["versions"]}, {"mark@claude-ai"})
        self.assertIn("goal-checkout", call("show_task", id=task)[0])  # the task links to its part of the plan
        text = call("show_goal", name="checkout")[0]
        self.assertIn("canvas goal-checkout", text)  # the goal's doc
        self.assertIn("The provider is Stripe", text)  # and its handoff
        self.assertIn("goal-checkout", call("list_docs", project="shop")[0])
        # A task's doc: it belongs to the task, and needs a name.
        self.assertIn("needs the document's name", call("start_doc", task=task)[0])
        self.assertFalse(call("start_doc", task=task, name="form-design")[1])
        self.assertEqual(core.canvas_show(c, "form-design")["item_id"], task)
        self.assertIn("form-design", call("show_task", id=task)[0])
        self.assertFalse(call("read_notes")[1])
        actors = {r["actor"] for r in c.execute("SELECT actor FROM events WHERE change LIKE 'canvas %'")}
        self.assertEqual(actors, {"mark@claude-ai"})
        # A person has no session of their own: the page opens the doc in the side panel it shows now.
        self.assertEqual(server.canvas_open(c, {"name": "goal-checkout"}, "mark")["session"], "")

    def test_agent_tools_are_refused(self):
        _, sid, call = self.session()
        for name, args in (("go", {}), ("done", {"id": 1, "output": "x"}), ("maxpm", {"args": ["claim", "1"]}),
                           ("manage", {}), ("browser", {"action": "open", "url": "https://example.com"})):
            text, bad = call(name, **args)
            self.assertTrue(bad)
            self.assertIn(f"{name} is not a planner tool", text)
        # A session of /mcp/plan never reaches /mcp's tools, nor the reverse.
        code, _, _ = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}, sid)
        self.assertEqual(code, 404)
        _, agent_sid, _ = self.session("/mcp")
        code, _, _ = self.request("POST", "/mcp/plan", {"jsonrpc": "2.0", "id": 6, "method": "tools/list"}, agent_sid)
        self.assertEqual(code, 404)
        self.assertEqual(self.request("DELETE", "/mcp", headers=sid)[0], 404)
        self.assertEqual(self.request("DELETE", "/mcp/plan", headers=sid)[0], 200)
        # Like /mcp: only this computer, straight to maxpm serve.
        code, r, _ = self.request("POST", "/mcp/plan", {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                                  {"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(code, 403)

    def test_through_the_relay_path(self):
        """A connector's request as the relay thread gives it to maxpm serve (relay.serve_page): Host 127.0.0.1, the
        relay's own header, the connector's headers without the hop's (the relay Worker strips X-Forwarded-*)."""
        from river import relay
        local = "http://127.0.0.1:8765"
        sent = relay.request_headers([["Content-Type", "application/json"], ["Accept", "application/json"],
                                      ["Mcp-Protocol-Version", "2025-06-18"], ["Host", "x.relay.maximizepm.com"]],
                                     local)
        hdrs = {**dict(sent), "X-Maxpm-Relay": relay.PAGE_SECRET}
        code, r, hd = self.request("POST", "/mcp/plan", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                         "params": {"clientInfo": {"name": "claude-ai"}}}, hdrs)
        self.assertEqual(code, 200, r)
        hdrs["Mcp-Session-Id"] = hd["Mcp-Session-Id"]
        _, r, _ = self.request("POST", "/mcp/plan", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "add_project", "arguments": {"name": "garden", "description": "The garden"}}}, hdrs)
        self.assertFalse(r["result"]["isError"], r)
        c = core.connect()
        self.assertTrue(c.execute("SELECT 1 FROM projects WHERE name='garden'").fetchone())
        c.close()

    def test_stdio_and_its_refusals(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "Antigravity CLI"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "add_project", "arguments": {"name": "notes", "description": "My notes"}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "edit_task", "arguments": {"id": 1}}}]

        def talk():
            out = io.StringIO()
            mcp.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out, plan=True)
            return [json.loads(x)["result"] for x in out.getvalue().splitlines()]
        r = talk()
        self.assertFalse(r[1]["isError"], r[1])
        self.assertIn("needs a field to change", r[2]["content"][0]["text"])
        c = core.connect()
        self.assertEqual(c.execute("SELECT actor FROM events WHERE change LIKE 'project notes added%'").fetchone()[0],
                         "mark@antigravity-cli")
        c.close()
        # Inside an agent session of MaximizePM the planner does not act as the person.
        os.environ["MAXPM_AGENT"] = "worker-1"
        self.addCleanup(os.environ.pop, "MAXPM_AGENT", None)
        msgs[1]["params"]["arguments"]["name"] = "notes2"
        r = talk()
        self.assertTrue(r[1]["isError"])
        self.assertIn("inside an agent session", r[1]["content"][0]["text"])

    def test_no_person_yet(self):
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "empty.db")
        srv = mcp.Server(plan=True)
        with self.assertRaisesRegex(core.RiverError, "has no person yet"):
            srv.call("list_projects", {})

    def test_client_slug(self):
        self.assertEqual(mcp.client_slug({"name": "claude-ai"}), "claude-ai")
        self.assertEqual(mcp.client_slug({"name": "OpenAI MCP Client"}), "openai-mcp-client")
        self.assertEqual(mcp.client_slug(None), "mcp")
        self.assertEqual(mcp.client_slug({"name": "!!!"}), "mcp")


if __name__ == "__main__":
    unittest.main()
