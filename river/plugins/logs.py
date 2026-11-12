"""The log check (#5463, mark 2026-11-12): the new lines of a log since the last run, against a pattern and limits.
Deterministic, no model call.

maxpm ops logs (--file <path> [--ssh <host>] | --command "<command>") [--pattern <regex>] [--ignore <regex>]
               [--warning N] [--critical N] [--per-minute] [--name <name>]

reads the lines that came since its last run, from one of three sources:

- a file on this computer (--file): from the byte where the last run stopped. A file that is shorter than that
  byte, or another file at the path (its inode), was rotated: the read starts at its beginning.
- a file on another host (--file with --ssh <host>): the same, through ssh in batch mode (no password prompt), so
  the host's key must let this computer in.
- a command (--command), such as journalctl or docker logs: its output, after the lines that the last run saw
  (the last LOOK_BACK lines of the last run are kept to find the place). {since} in the command is the time of the
  last run (RFC 3339, UTC) and {since_epoch} the same in seconds: docker logs app --since {since},
  journalctl -u app --since @{since_epoch}.

A line counts when it matches --pattern (a regular expression, default ERROR, CRITICAL, FATAL, Traceback, panic)
and not --ignore. With --warning or --critical, the run is WARNING or CRITICAL when the count reaches it (with
neither, one matching line is CRITICAL); with
--per-minute the limits are lines per minute since the last run. The first run of a check only finds the end of
the log, so old lines raise nothing (--from-start reads the old lines too). The output is on the Monitoring Plugins
API, so a plugin check runs it like any other plugin:

  LOGS OK|WARNING|CRITICAL|UNKNOWN - <summary> | matches=<n>;<warning>;<critical> lines=<n>
  {"matches": [the last MAX_EVIDENCE matching lines], "count": n, "lines": n, "skipped_bytes": n}

and exits 0, 1, 2, or 3 (the log could not be read). The read position of a check is in <queue folder>/logs/
<name>.json; its name is --name, else the check's own key that maxpm serve gives each run (MAXPM_CHECK_KEY)."""

import hashlib
import json
import os
import re
import shlex
import subprocess

from .. import core
from ..core import RiverError

DEFAULT_PATTERN = r"\b(ERROR|CRITICAL|FATAL|Traceback|panic)\b"
MAX_READ = 5 * 1024 * 1024  # the most bytes one run reads; past it, the oldest new bytes are skipped (and counted)
LOOK_BACK = 50              # the last lines of a command's output that the next run looks for
MAX_EVIDENCE = 20           # the matching lines that the evidence keeps
LINE_CHARS = 300            # the characters of one line of evidence
STATUS = {0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"}
RUNNER = None  # a test hook: (argv or shell text, timeout) -> (exit code, stdout bytes, stderr text)


def _state_file(name):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("._")
    if not safe:
        raise RiverError("name the log check: --name <name>, or run it as a plugin check (maxpm serve names it)")
    return core.db_path().parent / "logs" / f"{safe}.json"


def _run(cmd, timeout):
    if RUNNER:
        return RUNNER(cmd, timeout)
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RiverError(f"the log source did not answer within {timeout}s")
    except OSError as e:
        raise RiverError(f"the log source could not start: {e}")
    return r.returncode, r.stdout, r.stderr.decode(errors="replace")


def _read_file(path, state, max_read):
    """(new bytes, new state, skipped bytes) of a local file from the last offset."""
    try:
        st = os.stat(path)
    except OSError as e:
        raise RiverError(f"cannot read {path}: {e.strerror}")
    offset = state.get("offset")
    if offset is None:
        return None, {"offset": st.st_size, "ino": st.st_ino}, 0
    if st.st_size < offset or (state.get("ino") and state["ino"] != st.st_ino):
        offset = 0  # rotated
    start = max(offset, st.st_size - max_read)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(st.st_size - start)
    return data, {"offset": start + len(data), "ino": st.st_ino}, start - offset


def _read_remote(host, path, state, max_read, timeout):
    """The same through ssh: the first line of the answer is the file's size, the rest the new bytes."""
    offset = state.get("offset")
    q = shlex.quote(path)
    if offset is None:
        script = f"wc -c < {q}"
    else:
        script = (f"s=$(wc -c < {q}) || exit 3; echo $s; o={int(offset)}; [ $s -lt $o ] && o=0; "
                  f"[ $((s - o)) -gt {int(max_read)} ] && o=$((s - {int(max_read)})); echo $o; tail -c +$((o + 1)) {q}")
    code, out, err = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, script], timeout)
    if code != 0:
        raise RiverError(f"ssh {host} could not read {path}: {(err or '').strip()[:200] or 'exit ' + str(code)}")
    if offset is None:
        return None, {"offset": int(out.split()[0])}, 0
    size, _, rest = out.partition(b"\n")
    start, _, data = rest.partition(b"\n")
    size, start = int(size), int(start)
    from_ = offset if size >= offset else 0
    return data, {"offset": start + len(data)}, start - from_


def _key(line):
    return hashlib.sha1(line.encode(errors="replace")).hexdigest()[:16]


def _after(lines, seen):
    """The lines after the place where the last run's last lines end: the longest run of `seen` (oldest first) that
    the output holds, matched from its end. None of them in the output: every line is new."""
    if not seen:
        return lines
    keys = [_key(x) for x in lines]
    for n in range(min(len(seen), len(keys)), 0, -1):
        tail = seen[-n:]
        for i in range(len(keys) - n, -1, -1):
            if keys[i:i + n] == tail:
                return lines[i + n:]
    return lines


def _read_command(command, state, timeout, at):
    since = state.get("at") or core.iso(at)
    epoch = int(core.parse_iso(since).timestamp())
    cmd = command.replace("{since_epoch}", str(epoch)).replace("{since}", since)
    code, out, err = _run(cmd, timeout)
    if code != 0 and not out:
        raise RiverError(f"the command ended with exit {code}: {(err or '').strip()[:200]}")
    lines = out.decode(errors="replace").splitlines()
    first = "seen" not in state
    new = [] if first else _after(lines, state["seen"])
    return new, {"seen": [_key(x) for x in lines[-LOOK_BACK:]] if lines else state.get("seen", [])}, first


def run(name=None, file=None, ssh=None, command=None, pattern=None, ignore=None, warning=None, critical=None,
        per_minute=False, from_start=False, timeout=60, max_read=MAX_READ):
    """One read. Returns {"status": 0..3, "summary", "perf", "matches", "count", "lines", "skipped_bytes"}."""
    if bool(file) == bool(command):
        raise RiverError("give one source: --file <path> (with --ssh <host> for another host) or --command \"...\"")
    if ssh and not file:
        raise RiverError("--ssh reads a file on that host: give --file <path> too")
    try:
        pat = re.compile(pattern or DEFAULT_PATTERN)
        ign = re.compile(ignore) if ignore else None
    except re.error as e:
        raise RiverError(f"bad --pattern or --ignore: {e}")
    for v, k in ((warning, "--warning"), (critical, "--critical")):
        if v is not None and v <= 0:
            raise RiverError(f"{k} is more than 0: the count of matching lines that raises it")
    if warning is None and critical is None:
        critical = 1  # no limit given: one matching line is a fault
    sf = _state_file(name or os.environ.get("MAXPM_CHECK_KEY"))
    try:
        state = json.loads(sf.read_text())
    except (OSError, ValueError):
        state = {}
    at = core.now()
    if from_start and not state:
        state = {"offset": 0, "seen": []}
    source = f"{ssh}:{file}" if ssh else file or command
    try:
        if command:
            lines, new_state, first = _read_command(command, state, timeout, at)
            skipped = 0
        else:
            data, new_state, skipped = (_read_remote(ssh, file, state, max_read, timeout) if ssh
                                        else _read_file(file, state, max_read))
            first = data is None
            lines = [] if first else data.decode(errors="replace").splitlines()
    except RiverError as e:
        return {"status": 3, "summary": f"{source}: {e}", "perf": "", "matches": [], "count": 0, "lines": 0,
                "skipped_bytes": 0}
    minutes = max((at - core.parse_iso(state["at"])).total_seconds() / 60, 1 / 60) if state.get("at") else None
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps(dict(new_state, at=core.iso(at), source=source)))
    if first:
        return {"status": 0, "summary": f"{source}: the first read found the end of the log; from the next run on, "
                                        f"new lines count", "perf": "", "matches": [], "count": 0, "lines": 0,
                "skipped_bytes": 0}
    hits = [x for x in lines if pat.search(x) and not (ign and ign.search(x))]
    n = len(hits)
    value = n / minutes if per_minute and minutes else n
    unit = " a minute" if per_minute else ""
    status = 2 if critical is not None and value >= critical else 1 if warning is not None and value >= warning else 0
    rate = f" ({value:.1f}{unit})" if per_minute else ""
    summary = (f"{source}: {n} of {len(lines)} new lines match{rate}"
               + (f"; the limit is {critical if status == 2 else warning:g}{unit}" if status else "")
               + (f"; {skipped} older bytes skipped (more than {max_read} bytes came)" if skipped else ""))
    perf = (f"matches={n};{'' if warning is None else f'{warning:g}'};{'' if critical is None else f'{critical:g}'} "
            f"lines={len(lines)}")
    return {"status": status, "summary": summary, "perf": perf, "count": n, "lines": len(lines),
            "matches": [x[:LINE_CHARS] for x in hits[-MAX_EVIDENCE:]], "skipped_bytes": skipped}


def lines(res):
    """The output on the Monitoring Plugins API (module docstring)."""
    out = [f"LOGS {STATUS[res['status']]} - {res['summary']}" + (f" | {res['perf']}" if res.get("perf") else "")]
    if res["count"]:
        out.append(json.dumps({"matches": res["matches"], "count": res["count"], "lines": res["lines"],
                               "skipped_bytes": res["skipped_bytes"]}))
    return out
