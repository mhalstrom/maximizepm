"""The loop of maxpm serve in a process of its own (#5569): python -m river.loop serve <port>.

maxpm serve starts it (server.keep_loop) and starts it again when it ends. The passes (notify.loop) then run beside
the page's request threads, not in their process: a busy pass made a page action wait for Python's GIL at each
SQLite call (172 ms against 67 ms idle, #5567). serve writes to its stdin pipe and never closes it: the end of the
pipe (serve stopped, or restarted with exec) ends this loop after its pass. When the code on disk is ready to load
(server.reload_ready, against the code that serve runs: MAXPM_LOOP_BOOT and MAXPM_LOOP_CODE) and no hook or check of
this process runs, it exits with RESTART_EXIT, and serve starts again once its own hooks and checks end."""

import os
import sys
import threading

RESTART_EXIT = 75  # the exit code that asks maxpm serve to start again on the new code


def main(argv):
    if len(argv) != 2 or argv[0] != "serve" or not argv[1].isdigit():
        print("usage: python -m river.loop serve <port>  (maxpm serve starts it)", file=sys.stderr)
        return 2
    from . import notify, server
    notify.SERVE_PORT["port"] = int(argv[1])
    notify.CHILD["on"] = True
    # reload_ready compares the code on disk with the code that serve runs, not with this process's own.
    server.BOOT = os.environ.get("MAXPM_LOOP_BOOT") or server.BOOT
    server.BOOT_CODE = os.environ.get("MAXPM_LOOP_CODE") or server.BOOT_CODE
    server.DEV["on"] = os.environ.get("MAXPM_LOOP_DEV") == "1"
    # serve's process: a hook run of a serve that started again since (exec, the same pid) was cut (#5654).
    server.SERVE_PROCESS["id"] = os.environ.get("MAXPM_LOOP_SERVE") or server.SERVE_PROCESS["id"]
    stop = threading.Event()

    def parent_gone():
        # The raw descriptor, not sys.stdin: a thread blocked in a read of the buffered stdin holds its lock, and
        # Python aborts at the exit of a loop that asked for the restart (exit -6, 'could not acquire lock for
        # <stdin> at interpreter shutdown').
        try:
            fd = sys.stdin.fileno()
            while os.read(fd, 4096):  # b"" at the end of the pipe: serve ended or replaced itself
                pass
        except Exception:  # noqa: BLE001
            pass
        stop.set()
    threading.Thread(target=parent_gone, daemon=True, name="maxpm-loop-parent").start()
    notify.loop(stop)
    return RESTART_EXIT if notify.CHILD["restart"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
