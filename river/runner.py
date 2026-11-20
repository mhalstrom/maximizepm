"""A runner of maxpm commands inside maxpm serve (#5501): a process that keeps the code, one connection to the
queue and its caches, and runs one command at a time for a caller (river/client.py, through serve's POST /api/run).

The runner reads one request per line on its stdin and writes one answer per line on its stdout; each command runs
with the caller's words, folder, environment and parent process (core.CALLER_PPID: the agent CLI above the caller's
shell), and its output goes back in the answer. serve keeps a few runners (server.RUNNERS) and starts a new one
when one ends. A runner whose code is older than the caller's checkout says so ("stale"), and the caller runs the
command in its own process.

    python3 -m river.runner <queue file>"""
import contextlib
import gc
import io
import json
import os
import sys
import tempfile
import traceback


def _capture():
    """A file that takes what child processes write to fd 1 or 2 while a command runs."""
    return tempfile.TemporaryFile(mode="w+b")


def serve_requests(db, proto_in, proto_out):
    """Run requests until the input ends. db: the queue this runner serves; a request for another queue is
    refused (the caller runs it)."""
    from river import cli, client, core
    stamp = client.code_stamp()
    home = os.getcwd()
    base_env = dict(os.environ)
    core.RESIDENT["open"] = True
    conn = core.connect(db)
    core.RESIDENT.update(conn=conn, path=str(core.Path(db).expanduser().resolve()))
    warm(core, conn)
    # Garbage collection (#5522): the caches above make a large heap, and a full collection in the middle of a command
    # took 15-50 ms of it. The objects of the warm caches are frozen (no collection scans them again), and a command runs
    # with collection off; the runner collects after it has sent the answer, while the caller reads it.
    gc.freeze()
    for line in proto_in:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        ans = {"code": 0, "out": "", "err": ""}
        env = req.get("env") or {}
        try:
            want = core.Path(env.get("MAXPM_DB") or core.HOME_DB).expanduser().resolve()
        except OSError:
            want = None
        if req.get("stamp") != stamp:
            ans = {"refused": "stale"}
        elif str(want) != core.RESIDENT["path"]:
            ans = {"refused": "another queue"}
        else:
            gc.disable()
            try:
                ans = run_one(cli, core, conn, req, env)
            finally:
                gc.enable()
        os.environ.clear()
        os.environ.update(base_env)
        try:
            os.chdir(home)
        except OSError:
            pass
        try:
            proto_out.write(json.dumps(ans) + "\n")
            proto_out.flush()
        except BrokenPipeError:
            break
        gc.collect()


def warm(core, conn):
    """Fill the caches before the first command: the open items, the word table, and the words of every work
    item (the similar items of show and add, and search)."""
    tok = core.ANN_SHARE.set(core.RESIDENT.setdefault("share", {}))
    try:
        core.annotate(conn, closed=False)
        core._idf(conn)
        for title, context in conn.execute("SELECT title, context FROM items WHERE kind='work'").fetchall():
            core._words_of(title or "")
            core._words_of(context or "")
    except Exception:  # noqa: BLE001 - a cold cache only costs time
        pass
    finally:
        core.ANN_SHARE.reset(tok)


def run_one(cli, core, conn, req, env):
    """One command: its exit status and output."""
    try:
        os.chdir(req["cwd"])
    except (OSError, KeyError, TypeError):
        return {"refused": "no folder"}
    os.environ.clear()
    os.environ.update(env)
    core.CALLER_PPID = int(req.get("ppid") or 0) or None
    core.CALLER_TOKEN = req.get("caller") or None  # #5845: serve's one-time token of the caller's connection
    # #5972: the processes above the caller, which serve read from the kernel (a session token is checked on them).
    core.CALLER_CHAIN = [tuple(c) for c in req["chain"]] if isinstance(req.get("chain"), list) else None
    # #5975: the files that the caller read for the command (client.reads) go into a folder of this runner, and the
    # words name them; stdin that the caller read is the command's stdin.
    argv = list(req["argv"])
    tmp = tempfile.mkdtemp(prefix="maxpm-run-") if req.get("files") else None
    for i, f in enumerate(req.get("files") or []):
        sub = os.path.join(tmp, str(i))
        os.mkdir(sub)
        path = os.path.join(sub, os.path.basename(f.get("name") or "file") or "file")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f.get("text") or "")
        if isinstance(f.get("at"), int) and 0 <= f["at"] < len(argv):
            argv[f["at"]] = (f.get("prefix") or "") + path
        core.CALLER_FILES[path] = f.get("source") or path  # what the command records as its source
    # #6008: one poll of a wait; its state goes back to the caller with the answer.
    core.POLL = dict(req["poll"]) if isinstance(req.get("poll"), dict) else None
    saved_stdin = sys.stdin
    if isinstance(req.get("stdin"), str):
        sys.stdin = io.StringIO(req["stdin"])
    sys.argv = ["maxpm", *argv]
    out, err = io.StringIO(), io.StringIO()
    fd_out, fd_err = _capture(), _capture()
    saved = os.dup(1), os.dup(2)
    code = 0
    try:
        os.dup2(fd_out.fileno(), 1)
        os.dup2(fd_err.fileno(), 2)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.run(argv)
            except SystemExit as e:
                code = e.code
            except core.RiverError as e:  # as cli.main prints it
                print(f"{core.COMMAND}: {e}", file=sys.stderr)
                code = 2
            except Exception:  # noqa: BLE001 - the caller sees what went wrong, the runner goes on
                traceback.print_exc()
                code = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        os.close(saved[0])
        os.close(saved[1])
        core.CALLER_PPID = None
        core.CALLER_TOKEN = None
        core.CALLER_CHAIN = None
        sys.stdin = saved_stdin
        core.CALLER_FILES.clear()
        poll, core.POLL = core.POLL, None
        if tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        if conn.in_transaction:  # a command that stopped inside a transaction: none stays open for the next
            conn.rollback()
            for k in ("ann", "ann_open", "ann_kept", "ann_closed_kept", ("graph", True), ("graph", False)):  # they may hold changes that are gone
                core.RESIDENT.get("share", {}).pop(k, None)
    if isinstance(code, str):  # sys.exit("text")
        err.write(code + "\n")
        code = 1
    extra = []
    for f in (fd_out, fd_err):
        f.seek(0)
        extra.append(f.read().decode(errors="replace"))
        f.close()
    ans = {"code": int(code or 0), "out": out.getvalue() + extra[0], "err": err.getvalue() + extra[1]}
    if poll is not None:
        ans["poll"] = poll.get("state") or {}
    return ans


def main():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # after a chdir too
    db = sys.argv[1] if len(sys.argv) > 1 else None
    # The protocol keeps the runner's own stdin and stdout; the commands get nothing to read and write their
    # output into the answer.
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 0)
    os.dup2(null, 1)
    sys.stdin = open(os.devnull, encoding="utf-8")
    sys.stdout = io.TextIOWrapper(os.fdopen(1, "wb", buffering=0, closefd=False), write_through=True)
    from river import core
    serve_requests(db or str(core.db_path()), proto_in, proto_out)


if __name__ == "__main__":
    main()
