"""The fast start of the maxpm command (#5501): hand the words to the running maxpm serve, whose runners keep the
code, one database connection and their caches between commands, so a command answers in a few tens of
milliseconds instead of starting Python and the code each time.

bin/maxpm calls run_remote() first. It returns the exit status when serve ran the command, or None when the
command must run in its own process: serve does not run (no serve file, no answer), it refused the request
(every runner busy, older code than this checkout), the command is one that runs here (LOCAL), or MAXPM_LOCAL is
set. None never means the command ran: a command that serve took is never run a second time here.

This module imports nothing of the rest of maxpm, and only os, sys, json and socket: its own start is the cost."""
import json
import os
import socket
import sys

# The commands that serve's runners may run. serve runs outside an agent's sandbox, so none of them reads or
# writes a file that the caller names (those options are in FILE_WORDS), runs the caller's commands (review, ops),
# runs for a long time (serve, wait, the watches), takes the terminal (view), or reads stdin. Every other command
# runs in its own process, as before.
REMOTE = {"show", "next", "list", "status", "who", "inbox", "needs-you", "blockers", "search", "heartbeat", "now",
          "note", "edit", "add", "done", "release", "claim", "go", "send", "alert", "ask", "answer", "forward",
          "withdraw", "accept", "decline", "offer", "give", "split", "keep", "undep", "dep", "drop", "prio", "move",
          "push", "blocked", "unblock", "replanned", "reopen", "takeover", "undo-takeover", "synced", "thread",
          "register", "session", "unregister", "capacity", "log", "project", "goal", "target", "queue", "stop",
          "check", "backlog", "activate", "merge", "duplicates", "policy", "prompt", "health", "config", "change",
          "plan", "manage", "guide", "db", "ship", "notify", "artifacts", "usage"}
# Words that keep a command in its own process: it waits, it reads stdin, or it reads or writes a file it names.
FILE_WORDS = ("--wait", "--watch", "--from", "--file", "--put", "--out", "--summary-file")


def _command(words):
    """The command word after the global options, or None (help, the version, nothing)."""
    it = iter(words)
    for w in it:
        if w in ("-h", "--help", "--version"):
            return None
        if w == "--as":
            next(it, None)
        elif not w.startswith("-"):
            return w
    return None


def serve_file(env=None):
    """The file in which maxpm serve names its port and token, beside the queue it serves."""
    env = os.environ if env is None else env
    db = env.get("MAXPM_DB") or os.path.join("~", ".maximizepm", "maxpm.db")
    return os.path.expanduser(db) + "-serve.json"


def code_stamp(root=None):
    """The newest change time of the code (river/*.py and river/*/*.py): a runner with older code refuses."""
    root = root or os.path.dirname(os.path.abspath(__file__))
    best = 0
    for d in (root, *(os.path.join(root, x) for x in ("agents", "routes"))):
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.endswith(".py"):
                        best = max(best, e.stat().st_mtime_ns)
        except OSError:
            pass
    return best


def _post(port, body, token, timeout):
    """(status, answer) of POST /api/run on this computer: directly, else through the sandbox's HTTP proxy
    (an agent sandbox permits no direct connection; its proxy passes a host that the sandbox allows)."""
    req = (f"POST /api/run HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
           f"X-Maxpm-Token: {token}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n")
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=2)
        head = req
    except PermissionError:
        proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        if not proxy:
            return None, None
        from urllib.parse import unquote, urlsplit
        p = urlsplit(proxy)
        s = socket.create_connection((p.hostname, p.port or 80), timeout=2)
        head = req.replace("POST /api/run", f"POST http://127.0.0.1:{port}/api/run", 1)
        if p.username:
            import binascii
            cred = binascii.b2a_base64(f"{unquote(p.username)}:{unquote(p.password or '')}".encode(), newline=False)
            head += f"Proxy-Authorization: Basic {cred.decode()}\r\n"
    except OSError:
        return None, None
    try:
        try:
            s.sendall(head.encode() + b"\r\n" + body)
        except OSError:
            return None, None  # nothing reached serve
        s.settimeout(timeout)
        chunks = []
        try:
            while True:
                c = s.recv(65536)
                if not c:
                    break
                chunks.append(c)
        except OSError:
            if not chunks:
                return 599, b""  # serve may have taken it: not run again here
    finally:
        s.close()
    data = b"".join(chunks)
    top, _, rest = data.partition(b"\r\n\r\n")
    try:
        status = int(top.split(b" ", 2)[1])
    except (IndexError, ValueError):
        return None, None
    return status, rest


def run_remote(words, env=None, cwd=None):
    """Run the command in maxpm serve; its exit status, or None when it must run here (see the module text)."""
    env = dict(os.environ if env is None else env)
    if env.get("MAXPM_LOCAL") or os.name == "nt":
        return None
    cmd = _command(words)
    if cmd not in REMOTE or "-" in words or any(w.split("=", 1)[0] in FILE_WORDS for w in words):
        return None
    if cmd == "artifacts" and "show" in words:  # it prints the text of a file
        return None
    try:
        with open(serve_file(env)) as f:
            info = json.load(f)
        port, token = int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        cwd = cwd or os.getcwd()
    except OSError:
        return None
    body = json.dumps({"argv": list(words), "cwd": cwd, "env": env, "ppid": os.getppid(),
                       "stamp": code_stamp(), "stdin_tty": sys.stdin.isatty() if sys.stdin else False}).encode()
    status, answer = _post(port, body, token, timeout=None)
    if status is None or 400 <= status < 500 or status == 503:
        # No connection, a proxy or serve that refused it (403, an older serve: 404), older code or another queue
        # (409), every runner busy (503): serve did not run it, so it runs here.
        return None
    try:
        res = json.loads(answer)
    except ValueError:
        res = None
    if status != 200 or not isinstance(res, dict):
        # serve took the command and something went wrong in it: it is not run a second time here.
        why = (res or {}).get("error") if isinstance(res, dict) else None
        sys.stderr.write(f"maxpm: maxpm serve took the command but gave no answer ({why or status}); look at "
                         f"the queue before you run it again (MAXPM_LOCAL=1 runs commands without serve)\n")
        return 1
    if res.get("out"):
        sys.stdout.write(res["out"])
        sys.stdout.flush()
    if res.get("err"):
        sys.stderr.write(res["err"])
        sys.stderr.flush()
    return int(res.get("code") or 0)
