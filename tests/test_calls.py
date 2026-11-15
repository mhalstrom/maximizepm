"""The call record (#5911): the hooks of Claude Code, Codex and Grok send each tool call to maxpm serve, which stores
it in tool_sessions and tool_calls with its task, masked."""
import contextlib
import io
import json
import os
import shlex
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

from river import agents, cli, client, core, server, vault
from river.agents import claude as claude_agent, codex as codex_agent, grok as grok_agent

SECRET = b"dummy-secret-value-5911"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "t.db")
        env = mock.patch.dict(os.environ, {"MAXPM_DB": self.path})
        env.start()
        self.addCleanup(env.stop)
        self.c = core.connect(self.path)
        self.addCleanup(self.c.close)
        old = vault.BACKEND
        vault.BACKEND = vault.Files()
        self.addCleanup(setattr, vault, "BACKEND", old)
        core._CALL_MASK.update(key=None, forms=[], pruned=None)
        core.project_add(self.c, "p")
        core.register(self.c, "ag")
        self.item = core.item_add(self.c, "p", "a task", 2, "", "any", (), "t")["id"]
        core.claim(self.c, self.item, "ag")

    def secret(self, name="dummy", data=SECRET):
        vault.BACKEND.put(self.c, name, data)
        with core.tx(self.c):
            self.c.execute("INSERT INTO secrets (name, created_at) VALUES (?, ?)", (name, core.iso(core.now())))

    def claude(self, ev, ref="t1", tool="Bash", inp=None, **more):
        return {"hook_event_name": ev, "session_id": "s1", "tool_use_id": ref, "tool_name": tool,
                "tool_input": inp if inp is not None else {"command": "ls"}, **more}

    def record(self, payload, agent_type="claude", agent="ag", at=None):
        call = agents.get(agent_type).read_call(payload)
        return core.record_call(self.c, agent_type, call, agent=agent, at=at)


class ReadCall(unittest.TestCase):
    def test_claude(self):
        pre = claude_agent.read_call({"hook_event_name": "PreToolUse", "session_id": "s", "tool_use_id": "u",
                                      "tool_name": "Edit", "tool_input": {"file_path": "a"}})
        self.assertEqual(pre, {"phase": "pre", "session_id": "s", "ref": "u", "tool": "Edit",
                               "input": {"file_path": "a"}, "output": None, "ok": True})
        post = claude_agent.read_call({"hook_event_name": "PostToolUse", "session_id": "s", "tool_use_id": "u",
                                       "tool_name": "Edit", "tool_input": {}, "tool_response": {"ok": 1}})
        self.assertEqual((post["phase"], post["output"], post["ok"]), ("post", {"ok": 1}, True))
        fail = claude_agent.read_call({"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "error": "no"})
        self.assertEqual((fail["output"], fail["ok"], fail["session_id"]), ("no", False, None))
        self.assertIsNone(claude_agent.read_call({"hook_event_name": "Stop"}))

    def test_codex(self):
        post = codex_agent.read_call({"hook_event_name": "PostToolUse", "session_id": "th", "tool_use_id": "c1",
                                      "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": "x"})
        self.assertEqual((post["phase"], post["ref"], post["output"]), ("post", "c1", "x"))
        self.assertIsNone(codex_agent.read_call({"hook_event_name": "SessionStart"}))

    def test_grok(self):
        pre = grok_agent.read_call({"hookEventName": "pre_tool_use", "sessionId": "g", "toolUseId": "t",
                                    "toolName": "run_terminal_command", "toolInput": {"command": "ls"}})
        self.assertEqual((pre["phase"], pre["session_id"], pre["ref"], pre["tool"]),
                         ("pre", "g", "t", "run_terminal_command"))
        post = grok_agent.read_call({"hook_event_name": "PostToolUse", "sessionId": "g", "toolUseId": "t",
                                     "toolName": "x", "toolInput": {}, "toolResult": {"type": "Bash"}})
        self.assertEqual(post["output"], {"type": "Bash"})
        fail = grok_agent.read_call({"hookEventName": "post_tool_use_failure", "toolName": "x", "error": "e"})
        self.assertEqual((fail["ok"], fail["output"]), (False, "e"))

    def test_adapter_without_read_call(self):
        self.assertIsNone(agents.get("aider").read_call({"hook_event_name": "PreToolUse"}))
        self.assertEqual(agents.get("aider").call_hooks(["maxpm", "report"]), [])


class Hooks(unittest.TestCase):
    def test_claude_hook_file_has_a_call_hook_for_every_tool(self):
        hooks = claude_agent.hook_settings(["/bin/maxpm", "report"])["hooks"]
        for ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            group = [g for g in hooks[ev] if "--call" in g["hooks"][0]["command"]]
            self.assertEqual(len(group), 1, ev)
            self.assertEqual(group[0]["matcher"], "")
            self.assertEqual(group[0]["hooks"][0]["command"], "/bin/maxpm report --call claude")
        # the push guard before a shell command stays
        self.assertTrue(any(g["matcher"] == "Bash" and "--hook" in g["hooks"][0]["command"]
                            for g in hooks["PreToolUse"]))

    def test_grok_agent_file_has_the_call_hook(self):
        text = grok_agent.agent_definition("/py", "/s.py", "/bin/maxpm report --call grok")
        self.assertEqual(text.count("report --call grok"), 3)
        self.assertNotIn("--call", grok_agent.agent_definition("/py", "/s.py"))

    def test_codex_words_and_trust_hash(self):
        words = codex_agent.call_hooks(["/bin/maxpm", "report"])
        self.assertEqual(words[::2], ["-c", "-c", "-c"])
        self.assertTrue(words[1].startswith("hooks.PreToolUse=[{hooks=[{type=\"command\""))
        self.assertIn('command="/bin/maxpm report --call codex"', words[3])
        state = words[5]
        cmd = "/bin/maxpm report --call codex"
        for label in ("pre_tool_use", "post_tool_use"):
            self.assertIn(f'"/<session-flags>/config.toml:{label}:0:0"={{trusted_hash='
                          f'"{codex_agent.call_hook_hash(label, cmd)}"}}', state)
        # The hash is sha256 over the JSON of the hook's identity, sorted keys, no spaces (codex fingerprint.rs).
        import hashlib
        ident = ('{"event_name":"pre_tool_use","hooks":[{"async":false,"command":"' + cmd +
                 '","timeout":5,"type":"command"}]}')
        self.assertEqual(codex_agent.call_hook_hash("pre_tool_use", cmd),
                         "sha256:" + hashlib.sha256(ident.encode()).hexdigest())

    def test_codex_start_gets_the_call_hooks(self):
        ad = agents.get("codex")
        self.addCleanup(setattr, core, "CALL_HOOKS", core.CALL_HOOKS)
        core.CALL_HOOKS = True
        words = core.hook_words(ad)
        self.assertEqual(words[0], "-c")
        self.assertTrue(any("--call codex" in w for w in words))
        self.assertTrue(any(w.startswith("hooks.state=") for w in words))


class Record(Base):
    def test_before_and_after_make_one_row_with_task_and_duration(self):
        t = core.now()
        cid = self.record(self.claude("PreToolUse"), at=t)
        self.assertEqual(self.record(self.claude("PostToolUse", tool_response={"stdout": "a b"}),
                                     at=t + timedelta(milliseconds=250)), cid)
        [row] = core.calls_list(self.c)
        self.assertEqual((row["item_id"], row["agent"], row["agent_type"], row["session_id"], row["tool"]),
                         (self.item, "ag", "claude", "s1", "Bash"))
        self.assertEqual((row["status"], row["duration_ms"], row["seq"], row["source"]), ("ok", 250, 1, "command"))
        self.assertEqual(json.loads(row["params"]), {"command": "ls"})
        self.assertEqual(json.loads(row["result"]), {"stdout": "a b"})
        ses = self.c.execute("SELECT * FROM tool_sessions").fetchall()
        self.assertEqual(len(ses), 1)
        self.assertEqual((ses[0]["item_id"], ses[0]["next_seq"], ses[0]["agent"]), (self.item, 2, "ag"))

    def test_after_first_then_before(self):
        self.record(self.claude("PostToolUse", tool_response="out"))
        self.record(self.claude("PreToolUse"))
        [row] = core.calls_list(self.c)
        self.assertEqual((row["status"], row["result"], json.loads(row["params"])), ("ok", "out", {"command": "ls"}))

    def test_failure_and_order_and_repeat(self):
        self.record(self.claude("PreToolUse", ref="a"))
        self.record(self.claude("PostToolUseFailure", ref="a", error="exit 1"))
        self.record(self.claude("PreToolUse", ref="b"))
        rows = core.calls_list(self.c)
        self.assertEqual([r["seq"] for r in rows], [2, 1])
        self.assertEqual((rows[1]["status"], rows[1]["error"], rows[1]["result"]), ("error", "exit 1", None))
        self.assertEqual(rows[0]["supersedes"], rows[1]["id"])  # the same tool and params again

    def test_values_are_masked(self):
        self.secret()
        self.record(self.claude("PreToolUse", inp={"command": f"echo {SECRET.decode()}"}))
        self.record(self.claude("PostToolUse", tool_response={"stdout": SECRET.decode() + "\n"}))
        [row] = core.calls_list(self.c)
        self.assertNotIn(SECRET.decode(), row["params"] + row["result"])
        self.assertEqual(json.loads(row["params"]), {"command": "echo ***"})
        self.assertIn("***", row["result"])

    def test_a_byte_array_is_masked_as_text(self):
        # Grok's shell result has the output as byte numbers too
        self.secret()
        out = list(SECRET + b"\n")
        self.record({"hookEventName": "post_tool_use", "sessionId": "g", "toolUseId": "t", "toolName": "x",
                     "toolInput": {"bytes": out}, "toolResult": {"output": out, "exit_code": 0}}, "grok")
        [row] = core.calls_list(self.c)
        self.assertEqual(json.loads(row["result"]), {"output": "***\n", "exit_code": 0})
        self.assertEqual(json.loads(row["params"]), {"bytes": "***\n"})
        self.assertEqual(core._bytes_as_text([1, 2, 300]), [1, 2, 300])
        self.assertEqual(core._bytes_as_text([255, 254, 253, 252]), [255, 254, 253, 252])  # no UTF-8

    def test_large_param_is_a_hash_and_large_result_a_file(self):
        big = "x" * 600
        self.record(self.claude("PreToolUse", tool="Write", inp={"file_path": "a.txt", "content": big}))
        self.record(self.claude("PostToolUse", tool="Write", tool_response="y" * (core.CALL_RESULT_MAX + 1)))
        [row] = core.calls_list(self.c)
        p = json.loads(row["params"])
        self.assertEqual(p["file_path"], "a.txt")
        self.assertEqual(set(p["content"]), {"sha256", "size", "type"})
        self.assertEqual((p["content"]["size"], p["content"]["type"]), (602, "str"))
        self.assertIsNone(row["result"])
        with open(row["result_file"], encoding="utf-8") as f:
            self.assertEqual(len(f.read()), core.CALL_RESULT_MAX + 1)

    def test_mcp_source(self):
        self.record(self.claude("PreToolUse", tool="mcp__github__list_prs", inp={}))
        self.assertEqual(core.calls_list(self.c)[0]["source"], "mcp:github")
        self.assertEqual(core._call_source("Read"), "builtin")
        self.assertEqual(core._call_source("linear__save_issue"), "mcp:linear")

    def test_no_record_at_zero_days(self):
        core.config_set(self.c, "call_record_days", "0")
        self.assertIsNone(self.record(self.claude("PreToolUse")))
        self.assertEqual(core.calls_list(self.c), [])

    def test_prune_keeps_rows_and_ends_sessions(self):
        old = core.now() - timedelta(days=31)
        self.record(self.claude("PreToolUse"), at=old)
        self.record(self.claude("PostToolUse", tool_response="y" * (core.CALL_RESULT_MAX + 1)), at=old)
        f = core.calls_list(self.c)[0]["result_file"]
        self.record(self.claude("PreToolUse", ref="new"))
        self.assertEqual(core.prune_calls(self.c, at=core.now()), 1)
        rows = core.calls_list(self.c)
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[1]["params"], rows[1]["result_file"], rows[1]["tool"]), (None, None, "Bash"))
        self.assertIsNotNone(rows[0]["params"])
        self.assertFalse(os.path.exists(f))
        core.end_tool_sessions(self.c)
        self.assertIsNone(self.c.execute("SELECT ended_at FROM tool_sessions").fetchone()["ended_at"])  # still here
        core.release(self.c, self.item, actor="ag")
        core.unregister(self.c, "ag")
        core.end_tool_sessions(self.c)
        self.assertIsNotNone(self.c.execute("SELECT ended_at FROM tool_sessions").fetchone()["ended_at"])

    def test_cli_lists_the_calls_of_a_task(self):
        self.record(self.claude("PreToolUse"))
        self.record(self.claude("PostToolUse", tool_response="done"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["-q", "calls", "--item", str(self.item)])
        self.assertIn(f"ag #{self.item}", out.getvalue())
        self.assertIn('{"command": "ls"}', out.getvalue())
        cid = core.calls_list(self.c)[0]["id"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run(["-q", "calls", str(cid)])
        self.assertIn("result: done", out.getvalue())


class Serve(Base):
    def test_hook_body_reaches_store_call(self):
        sent = {}

        def post(port, body, token, timeout, path):
            sent.update(port=port, body=body, token=token, path=path)
            return 204, b""
        with open(client.serve_file(), "w", encoding="utf-8") as f:
            json.dump({"port": 4321, "token": "tok"}, f)
        payload = json.dumps(self.claude("PreToolUse")).encode()
        rc = client.call_hook("claude", stdin=io.BytesIO(payload), env={"MAXPM_DB": self.path, "MAXPM_AGENT": "ag"},
                              post=post)
        self.assertEqual(rc, 0)
        self.assertEqual((sent["port"], sent["token"], sent["path"]), (4321, "tok", "/api/call"))
        req = json.loads(sent["body"])
        self.assertEqual((req["agent_type"], req["agent"], req["payload"]["tool_name"]), ("claude", "ag", "Bash"))
        self.assertIsNotNone(server.store_call(self.c, req))
        self.assertEqual(core.calls_list(self.c)[0]["item_id"], self.item)

    def test_hook_never_fails(self):
        for data in (b"", b"not json", b"{bad"):
            self.assertEqual(client.call_hook("claude", stdin=io.BytesIO(data), env={"MAXPM_DB": "/nonexistent/x"}), 0)
        self.assertEqual(client.call_hook("aider", stdin=io.BytesIO(b"{}"), env={}), 0)
        self.assertIsNone(server.store_call(self.c, {"agent_type": "claude", "payload": {"hook_event_name": "Stop"}}))
        self.assertIsNone(server.store_call(self.c, {"agent_type": "nope", "payload": {}}))

    def test_route_needs_the_token(self):
        h = server.Handler.__new__(server.Handler)
        body = json.dumps({"agent_type": "claude", "agent": "ag", "payload": self.claude("PreToolUse")}).encode()
        h.path, h.client_address = "/api/call", ("127.0.0.1", 5)
        h.rfile = io.BytesIO(body)
        sent = []
        h._send = lambda code, b, *a, **k: sent.append(code)
        h._refuse = lambda: False
        took = []
        with mock.patch.object(server, "take_call", took.append):
            h.headers = {"Content-Length": str(len(body)), "X-Maxpm-Token": "wrong"}
            h.do_POST()
            h.rfile = io.BytesIO(body)
            h.headers = {"Content-Length": str(len(body)), "X-Maxpm-Token": server.RUN_TOKEN}
            h.do_POST()
        self.assertEqual(sent, [403, 204])
        self.assertEqual(len(took), 1)
        self.assertEqual(took[0]["payload"]["tool_use_id"], "t1")


if __name__ == "__main__":
    unittest.main()
