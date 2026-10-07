"""An MCP server (stdio) for agents that cannot run shell commands.

Run it with `maxpm mcp`. Every tool runs the same code as the `maxpm` command
and returns the same text, so an agent reads the same briefings either way.
The server remembers the agent name that `go`, `plan`, or `manage` gives, and
passes it as --as on later calls that do not name one. With MAXPM_CHAT=1 (the
Claude desktop entry that `maxpm setup-agent --claude-desktop` writes) the
session is a chat with no folder: go, plan, and manage brief it for that.
Standard library only.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import threading
import time

from . import __version__, cli, core
from .core import RiverError

PROTOCOL = "2025-06-18"

_AS = {"type": "string", "description": "your MaximizePM agent name (default: the one go or plan gave you)"}
TOOLS = [
    {"name": "maxpm",
     "description": "Run any maxpm command and get its text output, for example [\"go\"], "
                    "[\"done\", \"12\", \"--output\", \"what changed, commit id\"], or [\"guide\"].",
     "inputSchema": {"type": "object", "required": ["args"], "properties": {
         "args": {"type": "array", "items": {"type": "string"}, "description": "the words after `maxpm`"},
         "as": _AS,
         "cwd": {"type": "string", "description": "run in this folder (the project folder, for go and add)"}}}},
    {"name": "go",
     "description": "Start or continue work: MaximizePM names you, picks a role, claims an item, and prints a briefing "
                    "that ends with what to run next.",
     "inputSchema": {"type": "object", "properties": {
         "as": _AS, "project": {"type": "string"}, "cwd": {"type": "string", "description": "the project folder"}}}},
    {"name": "done",
     "description": "Finish an item you hold. Put what changed and the commit id in output.",
     "inputSchema": {"type": "object", "required": ["id", "output"], "properties": {
         "id": {"type": "integer"}, "output": {"type": "string"}, "as": _AS}}},
    {"name": "show",
     "description": "One item with its notes, links, and history.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}, "as": _AS}}},
    {"name": "goal",
     "description": "Goals: outcomes in a project that one agent owns. list (open goals with owner and progress), "
                    "show (a goal and its items), own (you plan and take the items that reach it; refused for a shared "
                    "goal, which has no owner and whose items are open to every agent), release, "
                    "or done (declare it complete with a one-line result; refused while its items are open).",
     "inputSchema": {"type": "object", "required": ["action"], "properties": {
         "action": {"type": "string", "enum": ["list", "show", "own", "release", "done"]},
         "name": {"type": "string", "description": "the goal (all actions except list)"},
         "project": {"type": "string", "description": "list: only this project's goals"},
         "result": {"type": "string", "description": "done: what the goal achieved, one line"},
         "as": _AS}}},
    {"name": "inbox",
     "description": "Your unread messages and the questions that wait for your answer.",
     "inputSchema": {"type": "object", "properties": {"as": _AS}}},
    {"name": "plan",
     "description": "Plan with the user: MaximizePM names you a planner and prints the overview, the open questions, "
                    "and the planner's rules (add items, dependencies, priorities).",
     "inputSchema": {"type": "object", "properties": {
         "as": _AS, "project": {"type": "string", "description": "project name(s) to focus on (default: all)"}}}},
    {"name": "manage",
     "description": "Run the other agents (one manager at a time): what needs attention (stuck agents, projects "
                    "with ready work and no agent, what waits on the user) and the manager's commands.",
     "inputSchema": {"type": "object", "properties": {
         "as": _AS, "takeover": {"type": "string", "description": "why you take over from the active manager"}}}},
]

INSTRUCTIONS = "Call go to take work from the MaximizePM queue, and follow its briefing."
CHAT_INSTRUCTIONS = ("MaximizePM is the user's work queue for AI agents. In this chat (no folder, no shell) you can: "
                     "plan work with the user (plan), run the coding agents (manage), go through what waits on the user "
                     "(maxpm tool: [\"needs-you\"], then [\"prompt\", \"--all\"]), and do items that need no code, "
                     "such as writing or research (go; the result goes in done). Call the tool, then follow its briefing; "
                     "for other maxpm commands use the maxpm tool with the words after `maxpm`.")


# One river command at a time: a call changes the process's folder and captures its stdout, and maxpm serve
# answers HTTP requests on several threads.
_CALL_LOCK = threading.Lock()


class Server:
    """chat: a session with no folder (default: MAXPM_CHAT=1); go, plan, and manage get --chat. folders: whether a
    tool call may name a cwd (not over HTTP: a web chat has no folder on this computer)."""

    def __init__(self, chat=None, folders=True, via=None):
        # Over HTTP the session is a new chat: not the agent whose environment started maxpm serve.
        self.agent = os.environ.get("MAXPM_AGENT") if folders else None
        self.chat = os.environ.get("MAXPM_CHAT") == "1" if chat is None else chat
        self.folders = folders
        self.via = via  # "relay": the session came through the relay; its events say <agent>@relay
        self.last_used = time.time()

    def argv(self, name, a):
        if name == "maxpm":
            words = [str(x) for x in a.get("args") or []]
        elif name == "go":
            words = ["go"] + (["--project", a["project"]] if a.get("project") else [])
        elif name == "done":
            words = ["done", str(a["id"]), "--output", a["output"]]
        elif name == "show":
            words = ["show", str(a["id"])]
        elif name == "goal":
            act = a["action"]
            if act not in ("list", "show", "own", "release", "done"):
                raise RiverError("goal action is one of list, show, own, release, done")
            words = ["goal", act]
            if act == "list":
                words += ["--project", a["project"]] if a.get("project") else []
            else:
                if not a.get("name"):
                    raise RiverError(f"goal {act} needs the goal name")
                words.append(a["name"])
            if act == "done":
                words += ["--result", a.get("result") or ""]
        elif name == "inbox":
            words = ["inbox"]
        elif name == "plan":
            words = ["plan"] + (["--project", a["project"]] if a.get("project") else [])
        elif name == "manage":
            words = ["manage"] + (["--takeover", a["takeover"]] if a.get("takeover") else [])
        else:
            raise RiverError(f"unknown tool {name!r}")
        if self.chat and words and words[0] in ("go", "plan", "manage") and "--chat" not in words:
            words = words[:1] + ["--chat"] + words[1:]
        who = a.get("as") or self.agent
        if who and "--as" not in words:
            words = ["--as", who] + words
        return words

    def call(self, name, a):
        """Run one river command in-process; return (text, is_error)."""
        words = self.argv(name, a)
        if a.get("cwd") and not self.folders:
            raise RiverError("this session has no folder on this computer; leave out cwd")
        with _CALL_LOCK:
            token = core.EVENT_VIA.set(self.via)
            try:
                return self._call(words, a)
            finally:
                core.EVENT_VIA.reset(token)

    def _call(self, words, a):
        out, err = io.StringIO(), io.StringIO()
        old = os.getcwd()
        # Over HTTP, river's own default name ($MAXPM_AGENT of maxpm serve) is not this chat's.
        env_agent = os.environ.pop("MAXPM_AGENT", None) if not self.folders else None
        code = 0
        try:
            if a.get("cwd"):
                os.chdir(os.path.expanduser(a["cwd"]))
            elif not self.folders:  # maxpm serve may run in a project folder; a web chat is in none
                os.chdir(os.path.abspath(os.sep))
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.run(words) or 0
                except RiverError as e:
                    print(f"maxpm: {e}", file=sys.stderr)
                    code = 2
                except SystemExit as e:  # argparse errors and --help
                    code = e.code if isinstance(e.code, int) else 2
        except OSError as e:
            err.write(f"maxpm: {e}\n")
            code = 2
        finally:
            os.chdir(old)
            if env_agent is not None:
                os.environ["MAXPM_AGENT"] = env_agent
        text = out.getvalue() + err.getvalue()
        m = re.search(r"^You are (?:MaximizePM agent|the MaximizePM MANAGER) (\S+?)(?: \(|\.)", text, re.M)
        if m:
            self.agent = m.group(1)
        return text.strip() or "(no output)", code != 0

    def handle(self, msg):
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            return None  # a notification, such as notifications/initialized
        if method == "initialize":
            result = {"protocolVersion": msg.get("params", {}).get("protocolVersion") or PROTOCOL,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "maximizepm", "version": __version__},
                      "instructions": CHAT_INSTRUCTIONS if self.chat else INSTRUCTIONS}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                text, bad = self.call(p.get("name"), p.get("arguments") or {})
            except (RiverError, KeyError) as e:
                text, bad = f"maxpm: {e}", True
            result = {"content": [{"type": "text", "text": text}], "isError": bad}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    srv = Server()
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = srv.handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


# ---------------------------------------------------------------- Streamable HTTP (maxpm serve, /mcp)

HTTP_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()
SESSION_IDLE = 7 * 24 * 3600  # an HTTP session unused this long is forgotten (relay sessions add up)
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
# Headers that proxies and tunnels add: a request that has one did not come from this computer directly.
PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip", "cf-connecting-ip", "cf-ray",
                 "cf-ipcountry", "true-client-ip")


def _host_only(value):
    v = (value or "").strip().lower()
    if v.startswith("["):
        return v[1:v.find("]")] if "]" in v else v
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def local_refusal(client_ip, headers, what="/mcp"):
    """Why a /mcp request is refused, or None. Until the endpoint has OAuth (#438) it answers only requests
    made on this computer, straight to maxpm serve: never through a tunnel or proxy, whose requests also
    arrive from 127.0.0.1. The Host and Origin checks stop a web page of another site from calling it (DNS
    rebinding). Every other route of maxpm serve (what) follows the same rule: server.Handler._refuse."""
    h = {k.lower(): v for k, v in headers.items()}
    if client_ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return f"only this computer may call {what}"
    if any(x in h for x in PROXY_HEADERS):
        return f"{what} has no sign-in yet, so it refuses requests through a tunnel or proxy"
    if _host_only(h.get("host")) not in LOCAL_HOSTS:
        return f"{what} answers only on 127.0.0.1 or localhost"
    origin = h.get("origin")
    if origin and _host_only(origin.split("://", 1)[-1].split("/", 1)[0]) not in LOCAL_HOSTS:
        return "cross-origin request refused"
    return None


def _expire_sessions():
    """Inside _SESSIONS_LOCK."""
    old = time.time() - SESSION_IDLE
    for sid in [sid for sid, srv in HTTP_SESSIONS.items() if srv.last_used < old]:
        del HTTP_SESSIONS[sid]


def http_post(body, session_id=None, via=None):
    """One POST to /mcp: returns (status, reply or None, headers). A JSON-RPC request gets its reply; a
    notification or a response gets 202 and no body. initialize starts a session (Mcp-Session-Id).
    via="relay": the request came over the relay socket (relay.py); its sessions are apart from local ones."""
    import secrets
    try:
        msg = json.loads(body or b"")
    except ValueError:
        return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, {}
    if not isinstance(msg, dict):
        return 400, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "send one JSON-RPC message per request"}}, {}
    headers = {}
    if msg.get("method") == "initialize":
        session_id = secrets.token_urlsafe(24)
        with _SESSIONS_LOCK:
            _expire_sessions()
            HTTP_SESSIONS[session_id] = Server(chat=True, folders=False, via=via)
        headers["Mcp-Session-Id"] = session_id
    with _SESSIONS_LOCK:
        srv = HTTP_SESSIONS.get(session_id) if session_id else None
        if srv is not None and srv.via != via:
            srv = None  # a local session id sent over the relay, or the other way round
        if srv is not None:
            srv.last_used = time.time()
    if srv is None:
        if not session_id:
            return 400, {"jsonrpc": "2.0", "id": msg.get("id"),
                         "error": {"code": -32600, "message": "no Mcp-Session-Id: initialize first"}}, {}
        return 404, {"jsonrpc": "2.0", "id": msg.get("id"),
                     "error": {"code": -32001, "message": "unknown session: initialize again"}}, {}
    if "method" not in msg or msg.get("id") is None:
        return 202, None, headers  # a notification, or a response to a request of ours (we send none)
    return 200, srv.handle(msg), headers


def http_delete(session_id, via=None):
    with _SESSIONS_LOCK:
        srv = HTTP_SESSIONS.get(session_id or "")
        if srv is None or srv.via != via:
            return False
        del HTTP_SESSIONS[session_id]
        return True
