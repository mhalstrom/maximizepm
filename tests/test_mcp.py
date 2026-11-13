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
        r = self.talk(call(1, {"action": "put", "name": "notes", "kind": "md", "text": "---\ntitle: x\n---\n# Notes",
                               "title": "My notes", "note": "-first"}),
                      call(2, {"action": "comment", "name": "notes", "text": "-shorter", "quote": "Notes"}, "mark"),
                      call(3, {"action": "send", "name": "notes"}, "mark"),
                      call(4, {"action": "comments", "name": "notes", "open": True}),
                      call(5, {"action": "reply", "comment": "c1", "text": "done in v2"}),
                      call(6, {"action": "resolve", "comment": "c1", "note": "v2"}),
                      call(7, {"action": "list"}),
                      call(8, {"action": "show"}),
                      call(9, {"action": "fly", "name": "notes"}))
        text = [x["result"]["content"][0]["text"] for x in r]
        self.assertEqual([x["result"]["isError"] for x in r], [False] * 7 + [True, True])
        self.assertIn('notes  v1 md  demo  #1  "My notes"', text[0])
        self.assertIn('c1 [read] mark on "Notes" (v1): -shorter', text[3])
        self.assertIn("needs the canvas name", text[7])
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


if __name__ == "__main__":
    unittest.main()
