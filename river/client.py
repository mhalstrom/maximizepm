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
          "withdraw", "accept", "decline", "offer", "give", "split", "keep", "undep", "dep", "drop", "prio",
          "push", "blocked", "unblock", "replanned", "reopen", "takeover", "thread",
          "register", "session", "unregister", "capacity", "log", "project", "goal", "target", "queue", "stop",
          "check", "backlog", "activate", "merge", "duplicates", "policy", "prompt", "health", "config",
          "plan", "manage", "guide", "db", "ship", "notify", "artifacts", "usage", "runs", "browser", "canvas",
          "scope", "pause", "resume", "pipeline"}
# #6009: commands of which only some subcommands go to serve. review pass runs the review command in the caller and
# sends its result (cli: op review_record); expert start and fresh open a terminal through serve already.
REMOTE_SUBS = {"review": {"fail"}, "expert": {None, "list", "adopt", "request", "end", "notes"}}
# #5975 (only serve writes the queue): a file that a command reads is read here, in the caller's own sandbox, and
# serve's runner gets its text in a file of its own; serve, outside every sandbox, never opens a path that a caller
# names. '-' (stdin) is read here too. Options per (command, subcommand) and positions per (command, subcommand).
READ_OPTS = {("add", None): ("--from",), ("goal", "handoff"): ("--file",), ("expert", "notes"): ("--put",),
             ("pipeline", "event"): ("--file",), ("pipeline", "report"): ("--steps", "--junit")}  # #6163
READ_POS = {("canvas", "put"): 0, ("canvas", "section"): 2}
READ_MAX = 8 << 20  # a larger file runs in the caller's process
CANVAS_LOCAL = {"save"}  # it writes a file
# Words that keep a command in its own process: it waits, it reads stdin, or it reads or writes a file it names.
FILE_WORDS = ("--wait", "--watch", "--from", "--file", "--put", "--out", "--summary-file")
# maxpm browser (#5665): a screenshot writes a PNG file, and batch reads stdin; they run in their own process. Its
# --wait is what one page action waits for (a load, seconds at most), not a long wait.
BROWSER_LOCAL = {"screenshot", "batch"}


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


def _at(words):
    """The index of the command word (see _command), or len(words)."""
    i = 0
    while i < len(words):
        if words[i] == "--as":
            i += 2
        elif words[i].startswith("-"):
            i += 1
        else:
            return i
    return len(words)


def _sub(words, i):
    """The first word after words[i] that is not an option, or None."""
    for w in words[i + 1:]:
        if not w.startswith("-"):
            return w
    return None


def reads(words):
    """(index, prefix, path) of each word that names a file the command reads (READ_OPTS, READ_POS): the word at
    index becomes prefix + the runner's own file. None when a word names one in a form this cannot read."""
    i = _at(words)
    if i >= len(words):
        return []
    cmd, sub = words[i], _sub(words, i)
    out = []
    opts = READ_OPTS.get((cmd, sub)) or READ_OPTS.get((cmd, None)) or ()
    for k in range(i + 1, len(words)):
        w = words[k]
        for o in opts:
            if w == o:
                if k + 1 >= len(words):
                    return None
                out.append((k + 1, "", words[k + 1]))
            elif w.startswith(o + "="):
                out.append((k, o + "=", w[len(o) + 1:]))
    pos = READ_POS.get((cmd, sub))
    if pos is not None:
        k, n = words.index(sub, i + 1) + 1, 0
        while k < len(words):
            w = words[k]
            if w in ("-q", "--quiet", "--json"):
                k += 1
            elif w.startswith("-") and w != "-":
                k += 1 if "=" in w else 2  # every other option of these subcommands takes a value
            elif n == pos:
                out.append((k, "", w))
                break
            else:
                n, k = n + 1, k + 1
    return out


def _local_pictures(text):
    """The page names a picture by a path (canvas put puts it inline from the page's folder, #4983): it runs in the
    caller's process, which can read that folder. Links to the web, data and anchors need nothing."""
    for mark in ('src="', "src='", "](", "url(", "href=\""):
        at = text.find(mark)
        while at >= 0:
            rest = text[at + len(mark):at + len(mark) + 12].lstrip("'\" <")
            if rest and not rest.startswith(("http:", "https:", "data:", "#", "//", "mailto:", "{", "$")):
                return True
            at = text.find(mark, at + 1)
    return False


def _inline(words, found):
    """The files and stdin of a request ({"files": [...], "stdin": text}), or None when one cannot be read here."""
    files, stdin = [], None
    for k, prefix, path in found:
        if path == "-":
            if stdin is None:
                if not sys.stdin or sys.stdin.isatty():
                    return None
                stdin = sys.stdin.read()
            continue
        try:
            if os.path.getsize(path) > READ_MAX:
                return None
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            return None
        if path.endswith((".md", ".html", ".htm", ".markdown")) and _local_pictures(text):
            return None
        files.append({"at": k, "prefix": prefix, "name": os.path.basename(path) or "file", "text": text,
                      "source": os.path.abspath(path)})
    return {"files": files, "stdin": stdin}


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


# Where a command goes: /api/run with the serve file's token; on a runner (#6151) /api/runner-run with the run's key,
# through the port of the ssh tunnel that maxpm serve opened (MAXPM_RUNNER_PORT, MAXPM_RUNNER_KEY).
ROUTE = {"path": "/api/run", "header": "X-Maxpm-Token"}
RUNNER_LOCAL = (None, "guide", "skills")  # what maxpm runs on a runner itself: help, the version, the guides


def _post(port, body, token, timeout):
    """(status, answer) of POST /api/run on this computer: directly, else through the sandbox's HTTP proxy
    (an agent sandbox permits no direct connection; its proxy passes a host that the sandbox allows)."""
    req = (f"POST {ROUTE['path']} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
           f"{ROUTE['header']}: {token}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n")
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
        head = req.replace(f"POST {ROUTE['path']}", f"POST http://127.0.0.1:{port}{ROUTE['path']}", 1)
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
    runner = env.get("MAXPM_RUNNER_PORT")
    if env.get("MAXPM_LOCAL") and not runner:
        return None
    cmd = _command(words)
    files = FILE_WORDS
    if cmd == "browser":
        if _command(words[_at(words) + 1:]) in BROWSER_LOCAL:
            return None
        files = tuple(w for w in FILE_WORDS if w != "--wait")
    poll = _pollable(cmd, words)
    if poll:
        files = tuple(w for w in FILE_WORDS if w not in ("--wait", "--watch"))
    elif cmd not in REMOTE and not _remote_sub(cmd, words):
        return None
    found = reads(words)
    if found is None:
        return None
    inlined = {k for k, _, _ in found} | {k - 1 for k, prefix, _ in found if not prefix}  # the values and options
    if any(k not in inlined and (w == "-" or w.split("=", 1)[0] in files) for k, w in enumerate(words)):
        return None
    if cmd == "artifacts" and "show" in words:  # it prints the text of a file
        return None
    if cmd == "canvas" and _sub(words, _at(words)) in CANVAS_LOCAL:
        return None
    if runner:
        try:
            port, token = int(runner), env.get("MAXPM_RUNNER_KEY") or ""
        except ValueError:
            return None
        ROUTE.update(path="/api/runner-run", header="X-Maxpm-Runner")
    else:
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
    extra = _inline(words, found) if found else {}
    if extra is None:
        return None
    req = {"argv": list(words), "cwd": cwd, "env": env, "ppid": os.getppid(), "pid": os.getpid(),
           "stamp": code_stamp(), "stdin_tty": sys.stdin.isatty() if sys.stdin else False, **extra}
    if poll:
        return _poll_loop(port, token, req)
    status, answer = _post_fresh(port, _dumps(req).encode(), token)
    if runner and status == 503:  # every command runner of serve is busy: a runner has no queue to run it on
        import time
        for _ in range(10):
            time.sleep(1)
            status, answer = _post_fresh(port, _dumps(req).encode(), token)
            if status != 503:
                break
    if runner and (status is None or 400 <= status < 500 or status == 503):
        try:
            why = _loads(answer or b"{}").get("error")
        except (ValueError, AttributeError):
            why = None
        sys.stderr.write(f"maxpm: maxpm serve did not take the command ({why or status or 'no connection'}); "
                         f"this runner has no queue of its own\n")
        return 2
    if status is None or 400 <= status < 500 or status == 503:
        # No connection, a proxy or serve that refused it (403, an older serve: 404), older code or another queue
        # (409), every runner busy (503): serve did not run it, so it runs here.
        if extra.get("stdin") is not None:  # the command here reads the text that this read from stdin
            import io
            sys.stdin = io.StringIO(extra["stdin"])
        return None
    return _answer(status, answer)


POLL_AGAIN = 75  # core.POLL_AGAIN: a poll that found nothing yet
POLL_SLEEP = 3.0  # seconds between two polls, as the loops of the waits in core
POLL_LOST = 120.0  # seconds that serve may not answer during a wait (a restart) before the wait runs here


def _post_fresh(port, body, token):
    """_post, asked again once when the runner had older code than this checkout (409 stale): serve stops that runner,
    and the next request starts one with the new code (#6018: after main moved, a wait fell back to its own
    process; with a queue that only serve writes, #5975, that fallback is refused)."""
    status, answer = _post(port, body, token, timeout=None)
    if status == 409 and b"stale" in (answer or b""):
        status, answer = _post(port, body, token, timeout=None)
    return status, answer


def _remote_sub(cmd, words):
    """A subcommand that serve runs (REMOTE_SUBS), or maxpm report --state (not the hooks' report --hook/--wake)."""
    if cmd == "report":
        return any(w == "--state" or w.startswith("--state=") for w in words) and not any(
            w in ("--hook", "--wake", "--call") for w in words)
    subs = REMOTE_SUBS.get(cmd)
    return subs is not None and _sub(words, _at(words)) in subs


def _pollable(cmd, words):
    """A wait that serve runs one poll at a time (#6008): maxpm wait, inbox --wait, manage --watch."""
    return cmd == "wait" or (cmd == "inbox" and "--wait" in words) or (cmd == "manage" and "--watch" in words)


def _poll_loop(port, token, req, sleep=None, clock=None):
    """Run a wait as polls in serve: each poll is a short command in a runner; this process sleeps between them and
    keeps the wait's state. The exit status of the last poll, or None when serve took no poll (it runs here)."""
    import time
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    req = dict(req, poll={"id": _poll_id(), "state": {}})
    first, lost = True, None
    while True:
        status, answer = _post_fresh(port, _dumps(req).encode(), token)
        if status is None or 400 <= status < 500 or status in (502, 503, 504, 599):
            # serve does not run it now: a restart (no connection, older code), every runner busy, a lost answer.
            # serve never answers 502 or 504: the sandbox's HTTP proxy does, when serve is down for a restart (#6030).
            if first and status != 599:
                return None
            lost = lost if lost is not None else clock()
            if clock() - lost > POLL_LOST:
                sys.stderr.write("maxpm: maxpm serve stopped answering during this wait; it goes on here\n")
                return None
            sleep(POLL_SLEEP)
            continue
        lost = None
        try:
            res = _loads(answer)
        except ValueError:
            res = None
        if status == 200 and isinstance(res, dict) and res.get("code") == POLL_AGAIN and not res.get("out"):
            first = False
            if isinstance(res.get("poll"), dict):
                req["poll"]["state"] = res["poll"]
            sleep(POLL_SLEEP)
            continue
        return _answer(status, answer)


def _poll_id():
    """An id for one wait (its token in agents.wait_token): a newer wait of the session ends this one."""
    return "".join(f"{b:02x}" for b in os.urandom(4))


def _answer(status, answer):
    """Print the answer of serve and return its exit status."""
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
WSL_ONLY = ("MaximizePM runs on Windows only inside WSL 2. Install WSL 2, then install and run maxpm in its Linux "
            "terminal: https://github.com/mhalstrom/maximizepm#windows")


def runner_refusal(words):
    """The error of a command that an agent on a runner cannot run (#6151): it would need the queue's computer (a wait
    in this process, a file it writes, the review command), and the runner has no queue."""
    return (f"maxpm: maxpm {_command(words) or ''} runs only on the computer of the queue, and this is runner "
            f"{os.environ.get('MAXPM_RUNNER') or '?'}. Do the task here and close it with maxpm done or maxpm release; "
            f"say what you need in a note (maxpm send note \"...\" --item <id>)\n")


def refuse_native_windows():
    """Stop at the start on native Windows, with the one line that says what to do."""
    if os.name == "nt":
        sys.stderr.write(f"maxpm: {WSL_ONLY}\n")
        sys.exit(2)


def agent_values(env=None):
    """A command in Hermes's terminal tool gets none of the session's MaximizePM variables (#5241): it would read
    the default queue as an unnamed agent. Then take them from the session that Hermes stored. Each value that the
    environment has already stays."""
    env = os.environ if env is None else env
    if not env.get("HERMES_SESSION_ID") or any(env.get(k) for k in ("MAXPM_AGENT", "MAXPM_DB", "MAXPM_TOKEN")):
        return
    from .agents import hermes
    for k, v in hermes.session_values(env).items():
        env.setdefault(k, v)


def main():
    """The installed maxpm command (the pyproject entry point, and bin/maxpm): serve runs it when it can, else
    maxpm here."""
    refuse_native_windows()
    agent_values()
    code = run_remote(sys.argv[1:])
    if code is not None:
        sys.exit(code)
    if os.environ.get("MAXPM_RUNNER_PORT") and _command(sys.argv[1:]) not in RUNNER_LOCAL:
        sys.stderr.write(runner_refusal(sys.argv[1:]))
        sys.exit(2)
    from .cli import main as local
    local()
