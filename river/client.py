"""The fast start of the maxpm command (#5501): hand the words to the running maxpm serve, whose runners keep the
code, one database connection and their caches between commands, so a command answers in a few tens of
milliseconds instead of starting Python and the code each time.

bin/maxpm calls run_remote() first. It returns the exit status when serve ran the command, or None when the
command must run in its own process: serve does not run (no serve file, no answer), it refused the request
(every runner busy, older code than this checkout), the command is one that runs here (LOCAL), or MAXPM_LOCAL is
set. None never means the command ran: a command that serve took is never run a second time here.

This module imports nothing of the rest of maxpm, and of the standard library only os and the C modules _socket
and _json: its own start is the cost. json and socket import re, enum and collections, about 6 ms of a 47 ms
command (#5525); bin/maxpm also starts Python with -S (no site), which then saves its 5 ms."""
import os
import sys

try:  # the C parts of json and socket, without the Python modules around them (#5525)
    import _json
    import _socket

    class _Scan:
        strict, object_hook, object_pairs_hook, parse_float, parse_int = True, None, None, float, int

        @staticmethod
        def parse_constant(name):
            raise ValueError(f"no JSON constant {name}")

    def _no_type(o):
        raise TypeError(f"{type(o).__name__} is not JSON")

    _SCAN = _json.make_scanner(_Scan())
    _ENCODE = _json.make_encoder({}, _no_type, _json.encode_basestring_ascii, None, ": ", ", ", False, False, True)

    def _dumps(obj):
        return "".join(_ENCODE(obj, 0))

    def _loads(text):
        text = text.decode() if isinstance(text, bytes) else text
        text = text.strip()
        try:
            obj, end = _SCAN(text, 0)
        except StopIteration:
            raise ValueError("no JSON value")
        if end != len(text):
            raise ValueError("more text after the JSON value")
        return obj

    _dumps({"a": [1, None, True]})  # another Python whose C parts differ falls back to json below
except Exception:  # noqa: BLE001 - no C parts here: the Python modules do the same, a little slower
    import json as _json_mod
    import socket as _socket
    _dumps, _loads = _json_mod.dumps, _json_mod.loads

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
    s = None
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", port))
        head = req
    except PermissionError:
        s.close()
        proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        if not proxy:
            return None, None
        import socket  # the proxy may have a name to resolve: the whole module, only on this path
        from urllib.parse import unquote, urlsplit
        p = urlsplit(proxy)
        try:
            s = socket.create_connection((p.hostname, p.port or 80), timeout=2)
        except OSError:
            return None, None
        head = req.replace("POST /api/run", f"POST http://127.0.0.1:{port}/api/run", 1)
        if p.username:
            import binascii
            cred = binascii.b2a_base64(f"{unquote(p.username)}:{unquote(p.password or '')}".encode(), newline=False)
            head += f"Proxy-Authorization: Basic {cred.decode()}\r\n"
    except OSError:
        if s is not None:
            s.close()
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
    if env.get("MAXPM_LOCAL"):
        return None
    cmd = _command(words)
    if cmd not in REMOTE or "-" in words or any(w.split("=", 1)[0] in FILE_WORDS for w in words):
        return None
    if cmd == "artifacts" and "show" in words:  # it prints the text of a file
        return None
    try:
        with open(serve_file(env), encoding="utf-8") as f:
            info = _loads(f.read())
        port, token = int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        cwd = cwd or os.getcwd()
    except OSError:
        return None
    body = _dumps({"argv": list(words), "cwd": cwd, "env": env, "ppid": os.getppid(),
                       "stamp": code_stamp(), "stdin_tty": sys.stdin.isatty() if sys.stdin else False}).encode()
    status, answer = _post(port, body, token, timeout=None)
    if status is None or 400 <= status < 500 or status == 503:
        # No connection, a proxy or serve that refused it (403, an older serve: 404), older code or another queue
        # (409), every runner busy (503): serve did not run it, so it runs here.
        return None
    try:
        res = _loads(answer)
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


# mark 2026-11-12: MaximizePM runs on Windows only inside WSL 2 (a Linux system); native Windows is not supported.
WSL_ONLY = ("MaximizePM runs on Windows only inside WSL 2. Install WSL 2 (wsl --install; "
            "https://learn.microsoft.com/windows/wsl/install), then install and run maxpm in its Linux terminal")


def refuse_native_windows():
    """Stop at the start on native Windows, with the one line that says what to do."""
    if os.name == "nt":
        sys.stderr.write(f"maxpm: {WSL_ONLY}\n")
        sys.exit(2)


def main():
    """The installed maxpm command (the pyproject entry point, and bin/maxpm): serve runs it when it can, else
    maxpm here."""
    refuse_native_windows()
    code = run_remote(sys.argv[1:])
    if code is not None:
        sys.exit(code)
    from .cli import main as local
    local()
