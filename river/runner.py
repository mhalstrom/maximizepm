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
        proto_out.write(json.dumps(ans) + "\n")
        proto_out.flush()
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
    sys.argv = ["maxpm", *req["argv"]]
    out, err = io.StringIO(), io.StringIO()
    fd_out, fd_err = _capture(), _capture()
    saved = os.dup(1), os.dup(2)
    code = 0
    try:
        os.dup2(fd_out.fileno(), 1)
        os.dup2(fd_err.fileno(), 2)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.run(req["argv"])
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
    return {"code": int(code or 0), "out": out.getvalue() + extra[0], "err": err.getvalue() + extra[1]}


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
    sys.stdin = open(os.devnull)
    sys.stdout = io.TextIOWrapper(os.fdopen(1, "wb", buffering=0, closefd=False), write_through=True)
    from river import core
    serve_requests(db or str(core.db_path()), proto_in, proto_out)


if __name__ == "__main__":
    main()
