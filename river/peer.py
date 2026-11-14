"""Which process sent a request, and the processes above it, as the kernel says (#5823; measured in #5821).

A session cannot change its parents: a command that an agent runs has the agent's program above it. maxpm serve
finds the process that holds the other end of a request's TCP connection (holder) and climbs its parents (chain);
the CLI climbs its own (chain(os.getpid())). Each entry is (pid, ppid, comm, start): the start time tells a reused
pid from the process that serve recorded.

macOS: libproc (proc_pidinfo, proc_pidfdinfo, proc_listallpids), 0.1 ms for a check; ps and lsof are 15 and 70 ms.
Linux (WSL 2): /proc. None when the kernel does not answer (the Claude Code sandbox refuses another process's
information): the caller refuses then, it never falls back to a typed name."""

import ctypes
import os
import struct
import sys

PROC_PIDLISTFDS = 1
PROC_PIDTBSDINFO = 3
PROC_PIDFDSOCKETINFO = 3
BSDINFO_SIZE = 136
FDINFO_SIZE = 8
SOCKINFO_SIZE = 792
FDTYPE_SOCKET = 2
SOCKINFO_TCP = 2
# Byte offsets in the arm64 and x86_64 macOS structs (measured in #5821): proc_bsdinfo, socket_fdinfo.
PPID_AT, COMM_AT, START_AT = 16, 48, 120
FAMILY_AT, KIND_AT, FPORT_AT, LPORT_AT = 184, 256, 264, 268
CHAIN_MAX = 40

_LIB = {}


def _lib():
    if "lib" not in _LIB:
        try:
            lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            lib.proc_pidfdinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
            lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        except OSError:
            lib = None
        _LIB["lib"] = lib
    return _LIB["lib"]


def _mac_info(pid):
    lib = _lib()
    buf = ctypes.create_string_buffer(BSDINFO_SIZE)
    if not lib or lib.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, buf, BSDINFO_SIZE) != BSDINFO_SIZE:
        return None
    raw = buf.raw
    ppid = struct.unpack_from("<I", raw, PPID_AT)[0]
    comm = raw[COMM_AT:COMM_AT + 16].split(b"\0", 1)[0].decode(errors="replace")
    start = struct.unpack_from("<Q", raw, START_AT)[0]
    return pid, ppid, comm, start


def _linux_info(pid):
    try:
        text = open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    comm = text[text.index("(") + 1:text.rindex(")")]
    rest = text[text.rindex(")") + 2:].split()
    return pid, int(rest[1]), comm[:16], int(rest[19])  # state ppid ... starttime (field 22 of stat)


def info(pid):
    """(pid, ppid, comm, start) of a process, or None."""
    if not pid or pid < 0:
        return None
    return _mac_info(pid) if sys.platform == "darwin" else _linux_info(pid)


def chain(pid):
    """The process and its parents, up to launchd or init: [(pid, ppid, comm, start)], or None when the kernel does
    not answer for one of them (a sandbox): a chain with a gap could hide the agent above it."""
    out, seen = [], set()
    while pid and pid not in seen and len(out) < CHAIN_MAX:
        seen.add(pid)
        i = info(pid)
        if i is None:
            return None
        out.append(i)
        if i[1] <= 1:
            break
        pid = i[1]
    return out


def _mac_pids():
    lib = _lib()
    if not lib:
        return []
    n = lib.proc_listallpids(None, 0)
    buf = (ctypes.c_int * (n + 64))()
    n = lib.proc_listallpids(buf, ctypes.sizeof(buf))
    return [p for p in buf[:max(n, 0)] if p > 0]


def _mac_holds(pid, local_port, remote_port):
    """Whether pid has a TCP socket from local_port to remote_port."""
    lib = _lib()
    size = lib.proc_pidinfo(pid, PROC_PIDLISTFDS, 0, None, 0)
    if size <= 0:
        return False
    buf = ctypes.create_string_buffer(size + FDINFO_SIZE * 16)
    size = lib.proc_pidinfo(pid, PROC_PIDLISTFDS, 0, buf, len(buf))
    raw = buf.raw
    sock = ctypes.create_string_buffer(SOCKINFO_SIZE)
    for at in range(0, max(size, 0), FDINFO_SIZE):
        fd, kind = struct.unpack_from("<iI", raw, at)
        if kind != FDTYPE_SOCKET:
            continue
        if lib.proc_pidfdinfo(pid, fd, PROC_PIDFDSOCKETINFO, sock, SOCKINFO_SIZE) != SOCKINFO_SIZE:
            continue
        s = sock.raw
        if struct.unpack_from("<i", s, KIND_AT)[0] != SOCKINFO_TCP:
            continue
        if (struct.unpack_from(">H", s, LPORT_AT)[0] == local_port
                and struct.unpack_from(">H", s, FPORT_AT)[0] == remote_port):
            return True
    return False


def _linux_inode(local_port, remote_port):
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = open(f, encoding="ascii").read().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            w = ln.split()
            if len(w) > 9 and int(w[1].rsplit(":", 1)[1], 16) == local_port \
                    and int(w[2].rsplit(":", 1)[1], 16) == remote_port:
                return w[9]
    return None


def _linux_holds(pid, inode):
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                if os.readlink(f"/proc/{pid}/fd/{fd}") == f"socket:[{inode}]":
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def lookable(local_port, remote_port):
    """Whether this process can look for the holder of the connection at all: libproc loads (macOS), or the kernel
    lists the connection (Linux). Only then does a holder of None mean that no process of this user holds it."""
    if sys.platform == "darwin":
        return _lib() is not None
    return _linux_inode(local_port, remote_port) is not None


def holder(local_port, remote_port, hints=()):
    """The pid of the process that holds the TCP connection from local_port (the client's) to remote_port (serve's):
    the hinted pids first (the pid that the CLI says, the recorded agents' pids: the Claude Code sandbox proxy runs
    inside the session's own process), then every process. None when no process holds it."""
    if sys.platform == "darwin":
        if not _lib():
            return None
        test = lambda p: _mac_holds(p, local_port, remote_port)  # noqa: E731
        everyone = _mac_pids
    else:
        inode = _linux_inode(local_port, remote_port)
        if not inode:
            return None
        test = lambda p: _linux_holds(p, inode)  # noqa: E731
        everyone = lambda: [int(x) for x in os.listdir("/proc") if x.isdigit()]  # noqa: E731
    tried = set()
    for group in (hints, None):
        for p in (group if group is not None else everyone()):
            try:
                p = int(p)
            except (TypeError, ValueError):
                continue
            if p in tried or p == os.getpid():
                continue
            tried.add(p)
            if test(p):
                return p
    return None
