"""An MCP server (stdio) for agents that cannot run shell commands.

Run it with `maxpm mcp`. Every tool runs the same code as the `maxpm` command
and returns the same text, so an agent reads the same briefings either way.
The server remembers the agent name that `go` or `manage` gives, and
passes it as --as on later calls that do not name one. With MAXPM_CHAT=1 (the
Claude desktop entry that `maxpm setup-agent --claude-desktop` writes) the
session is a chat with no folder: go and manage brief it for that.
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
    {"name": "canvas",
     "description": "Canvases: pages (HTML, Markdown, or SVG) that you make for the user and MaximizePM keeps on this "
                    "computer, with versions; the user sees them in the MaximizePM page and comments on a part of a "
                    "page or on the whole page. put (a new version; the same name in the same project is the same "
                    "canvas), list, show (a version's text, its versions and comments), comments (the comments with "
                    "their replies; open: only the ones that wait for you), comment, send (your draft comments to the "
                    "session that wrote it), reply, resolve, delete, open (put it in front of the user: the MaximizePM page opens it in your session's side panel), save (a version as a file), "
                    "section (a doc: replace or add one section of a Markdown canvas; pending shows it as being written), "
                    "answer (a decision of a doc, answered in place), kit (the starter page of the canvas kit: a "
                    "visual HTML page with a task graph, cards, side by side, bars, a timeline, a mockup, pictures "
                    "with notes, and decision cards; fill it and put it).",
     "inputSchema": {"type": "object", "required": ["action"], "properties": {
         "action": {"type": "string", "enum": ["put", "list", "show", "comments", "comment", "send", "reply",
                                                "resolve", "open", "delete", "save", "section", "answer", "kit"]},
         "heading": {"type": "string", "description": "section: the heading of the section to replace or add (a doc)"},
         "pending": {"type": "string", "description": "section: what the section will say; the page shows it as being "
                                                      "written"},
         "decision": {"type": "integer", "description": "answer: the decision's number (Decision <n> of <m>)"},
         "name": {"type": "string", "description": "the canvas (all actions except list, reply, resolve, kit)"},
         "text": {"type": "string", "description": "put: the page's whole text; comment and reply: the comment"},
         "kind": {"type": "string", "enum": ["html", "md", "svg"], "description": "put: the kind of a new canvas"},
         "title": {"type": "string"}, "note": {"type": "string", "description": "put: what changed; resolve: what "
                                                                                 "changed, or why nothing needs to"},
         "item": {"type": "integer", "description": "put: the item it belongs to (default: the item you hold); "
                                                    "list: only this item's"},
         "project": {"type": "string"}, "version": {"type": "integer"},
         "open": {"type": "boolean", "description": "comments: only the ones that wait for an answer"},
         "comment": {"type": "string", "description": "reply and resolve: the comment id, for example c12"},
         "quote": {"type": "string", "description": "comment: the words of the page it is about"},
         "path": {"type": "string", "description": "comment: the part's CSS path; save: the file to write"},
         "session": {"type": "string", "description": "open: the agent whose side panel shows it (default: you)"},
         "as": _AS}}},
    {"name": "inbox",
     "description": "Your unread messages and the questions that wait for your answer.",
     "inputSchema": {"type": "object", "properties": {"as": _AS}}},
    {"name": "manage",
     "description": "Run the other agents (one manager at a time): what needs attention (stuck agents, projects "
                    "with ready work and no agent, what waits on the user) and the manager's commands.",
     "inputSchema": {"type": "object", "properties": {
         "as": _AS, "takeover": {"type": "string", "description": "why you take over from the active manager"}}}},
]

INSTRUCTIONS = "Call go to take work from the MaximizePM queue, and follow its briefing."
CHAT_INSTRUCTIONS = ("MaximizePM is the user's work queue for AI agents. In this chat (no folder, no shell) you can: "
                     "run the coding agents and plan work with the user (manage), go through what waits on the user "
                     "(maxpm tool: [\"needs-you\"], then [\"prompt\", \"--all\"]), and do items that need no code, "
                     "such as writing or research (go; the result goes in done). Call the tool, then follow its briefing; "
                     "for other maxpm commands use the maxpm tool with the words after `maxpm`.")


def canvas_argv(a):
    """The maxpm canvas words of an MCP canvas call: the same operations as the command (#4648)."""
    act = a.get("action")
    acts = ("put", "list", "show", "comments", "comment", "send", "reply", "resolve", "open", "delete", "save",
            "section", "answer", "kit")
    if act not in acts:
        raise RiverError(f"canvas action is one of {', '.join(acts)}")
    words = ["canvas", act]
    if act in ("reply", "resolve"):
        if not a.get("comment"):
            raise RiverError(f"canvas {act} needs the comment id (comment), for example c12")
        words.append(str(a["comment"]))
    elif act not in ("list", "kit"):
        if not a.get("name"):
            raise RiverError(f"canvas {act} needs the canvas name (name)")
        words.append(str(a["name"]))
    if act in ("comment", "reply"):
        text = a.get("text") or ""
        words.append(" " + text if text.startswith("-") else text)  # argparse reads a leading - as an option
    if act == "save":
        if not a.get("path"):
            raise RiverError("canvas save needs the file to write (path)")
        words.append(" " + a["path"] if a["path"].startswith("-") else a["path"])
    if act == "put":
        if a.get("text") is None:
            raise RiverError("canvas put needs the page's whole text (text)")
        words += [f"--name={words.pop()}", f"--text={a['text']}"]
    if act == "section":
        if not a.get("heading"):
            raise RiverError("canvas section needs the section's heading (heading)")
        words.append(" " + a["heading"] if a["heading"].startswith("-") else a["heading"])
        if a.get("pending") is not None:
            words.append(f"--pending={a['pending']}")
        elif a.get("text") is not None:
            words.append(f"--text={a['text']}")
        else:
            raise RiverError("canvas section needs the section's text (text) or what it will say (pending)")
    if act == "answer":
        if a.get("decision") is None or not a.get("text"):
            raise RiverError("canvas answer needs the decision's number (decision) and the answer (text)")
        words += [str(int(a["decision"])), " " + a["text"] if a["text"].startswith("-") else a["text"]]
    opts = {"put": ("kind", "title", "note", "item", "project"), "list": ("project", "item"),
            "show": ("version", "project"), "comments": ("project",), "comment": ("quote", "path", "version", "project"),
            "send": ("project",), "resolve": ("note",),
            "section": ("note", "project"), "answer": ("project",), "open": ("project", "session"), "delete": ("project",), "save": ("version", "project")}
    for k in opts.get(act, ()):
        if act == "save" and k == "path":
            continue
        if a.get(k) not in (None, ""):
            words += [f"--{k}={a[k]}"]
    if act == "comments" and a.get("open"):
        words.append("--open")
    return words


# One river command at a time: a call changes the process's folder and captures its stdout, and maxpm serve
# answers HTTP requests on several threads.
_CALL_LOCK = threading.Lock()


class Server:
    """chat: a session with no folder (default: MAXPM_CHAT=1); go, plan, and manage get --chat. folders: whether a
    tool call may name a cwd (not over HTTP: a web chat has no folder on this computer)."""

    def __init__(self, chat=None, folders=True):
        # Over HTTP the session is a new chat: not the agent whose environment started maxpm serve.
        self.agent = os.environ.get("MAXPM_AGENT") if folders else None
        self.chat = os.environ.get("MAXPM_CHAT") == "1" if chat is None else chat
        self.folders = folders

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
        elif name == "canvas":
            words = canvas_argv(a)
        elif name == "inbox":
            words = ["inbox"]
        elif name == "manage":
            words = ["manage"] + (["--takeover", a["takeover"]] if a.get("takeover") else [])
        else:
            raise RiverError(f"unknown tool {name!r}")
        if self.chat and words and words[0] in ("go", "manage") and "--chat" not in words:
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
            # A chat over HTTP runs inside maxpm serve: go takes nothing from serve's environment (core.HTTP_CHAT).
            token = core.HTTP_CHAT.set(None if self.folders else "http")
            try:
                return self._call(words, a)
            finally:
                core.HTTP_CHAT.reset(token)

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


def http_post(body, session_id=None):
    """One POST to /mcp: returns (status, reply or None, headers). A JSON-RPC request gets its reply; a
    notification or a response gets 202 and no body. initialize starts a session (Mcp-Session-Id)."""
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
            HTTP_SESSIONS[session_id] = Server(chat=True, folders=False)
        headers["Mcp-Session-Id"] = session_id
    with _SESSIONS_LOCK:
        srv = HTTP_SESSIONS.get(session_id) if session_id else None
    if srv is None:
        if not session_id:
            return 400, {"jsonrpc": "2.0", "id": msg.get("id"),
                         "error": {"code": -32600, "message": "no Mcp-Session-Id: initialize first"}}, {}
        return 404, {"jsonrpc": "2.0", "id": msg.get("id"),
                     "error": {"code": -32001, "message": "unknown session: initialize again"}}, {}
    if "method" not in msg or msg.get("id") is None:
        return 202, None, headers  # a notification, or a response to a request of ours (we send none)
    return 200, srv.handle(msg), headers


def http_delete(session_id):
    with _SESSIONS_LOCK:
        return HTTP_SESSIONS.pop(session_id or "", None) is not None
