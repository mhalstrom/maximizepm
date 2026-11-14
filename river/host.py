"""maxpm host setup <ssh target>: MaximizePM on a remote host, set up from this computer over SSH (#5876).

mark 2026-11-14: 'a process that sets it up via tail scale and SSH is perfectly fine', and (#5880) 'all the work is
done on the server': the whole queue and every project's folder live on one host; other devices are screens (the
page over Tailscale). The steps, each over `ssh -o BatchMode=yes <target>` (Tailscale SSH or a key; never a
password prompt):

1. check: the host answers, and runs Linux or macOS.
2. install: when the host has no maxpm, or another version, or --install: this computer's code (bin, river, skills,
   install-standalone.sh) goes over the SSH connection and install-standalone.sh --from installs it there, with its
   own Python (#5873). The same code on both sides, so a moved queue matches the code that reads it.
3. move (--move): this queue (and its archive) goes to the host, which must have no projects yet: MaximizePM never
   merges two queues. This computer's queue is then marked moved, and its maxpm refuses every command with the
   host's address, until maxpm host forget.
4. serve at login on the host (maxpm serve --install-login: systemd with linger, or launchd, #5875).
5. the page over Tailscale on the host (maxpm phone --on): its address, and its QR code for the phone.

Project folders do not move: the person clones each project on the host and runs maxpm project path there.
Standard library only."""

from __future__ import annotations

import io
import json
import os
import shlex
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path

from . import __version__, core
from .core import RiverError

SSH_RUNNER = None  # tests: fn(argv, data: bytes | None, timeout) -> (returncode, stdout text, stderr text)
ROOT = Path(__file__).resolve().parent.parent
MOVED_KEY = "moved_to_host"  # meta: {"target", "url", "at"} once this queue went to a host
# A non-interactive SSH command gets a short PATH: the launcher of install-standalone.sh is in ~/.local/bin.
REMOTE_MAXPM = 'PATH="$HOME/.local/bin:$PATH" maxpm --quiet'


def _ssh3(target, command, data=None, timeout=120):
    """(exit code, stdout, stderr) of a command on the host."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", target, command]
    if SSH_RUNNER is not None:
        return SSH_RUNNER(argv, data, timeout)
    try:
        r = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"ssh {target} gave no answer in {timeout} seconds"
    except OSError as e:
        return 127, "", f"ssh did not start: {e}"
    return r.returncode, r.stdout.decode(errors="replace").strip(), r.stderr.decode(errors="replace").strip()


def _ssh(target, command, data=None, timeout=120):
    """(exit code, stdout and stderr in one text): for steps whose output a person reads."""
    code, out, err = _ssh3(target, command, data, timeout)
    return code, "\n".join(x for x in (out, err) if x)


def _json(target, *args, timeout=120):
    """The --json reply of a maxpm command on the host, or (None, the error text)."""
    code, out, err = _ssh3(target, f"{REMOTE_MAXPM} --json {shlex.join(args)}", timeout=timeout)
    if code == 0:
        try:
            return json.loads(out), ""
        except ValueError:
            pass
    return None, (err or out or f"exit {code}")


def _maxpm(target, *args, data=None, timeout=120):
    return _ssh(target, f"{REMOTE_MAXPM} {shlex.join(args)}", data, timeout)


def _check_target(target):
    if not target or target.startswith("-") or any(c.isspace() for c in target):
        raise RiverError(f"{target!r} is not an SSH target; give one such as mini or you@server")


def check(target):
    """The host answers over SSH with no prompt; returns its system (uname -sm)."""
    code, out = _ssh(target, "uname -sm", timeout=30)
    if code:
        raise RiverError(f"ssh {target} does not answer without a prompt ({out or 'exit ' + str(code)}). Turn on "
                         f"Tailscale SSH on the host (tailscale set --ssh) or add your SSH key there, check that "
                         f"'ssh {target} true' works, then run maxpm host setup {target} again")
    system = out.splitlines()[-1] if out else ""
    if not system.startswith(("Linux", "Darwin")):
        raise RiverError(f"{target} runs {system or 'an unknown system'}; MaximizePM runs on Linux and macOS")
    return system


def host_version(target):
    """'maxpm 1.2.0' of the host's maxpm, or None when it has none."""
    code, out = _maxpm(target, "--version", timeout=30)
    last = out.splitlines()[-1].strip() if out else ""
    return last if code == 0 and last.startswith("maxpm ") else None


def code_archive():
    """This computer's code as a tar.gz: bin, river, skills and install-standalone.sh (no caches)."""
    if not (ROOT / "install-standalone.sh").is_file():
        raise RiverError(f"{ROOT} has no install-standalone.sh: this MaximizePM is not a checkout or a release "
                         f"archive; install MaximizePM on the host with install-standalone.sh, then run maxpm host "
                         f"setup again")
    buf = io.BytesIO()

    def skip(info):
        return None if "__pycache__" in info.name or info.name.endswith(".pyc") else info

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ("bin", "river", "skills", "install-standalone.sh"):
            if (ROOT / name).exists():
                tar.add(ROOT / name, arcname=name, filter=skip)
    return buf.getvalue()


def install(target):
    """install-standalone.sh --from this computer's code, on the host."""
    script = ('set -e; d=$(mktemp -d); trap \'rm -rf "$d"\' EXIT; tar -xzf - -C "$d"; '
              'sh "$d/install-standalone.sh" --from "$d"')
    code, out = _ssh(target, script, code_archive(), timeout=600)
    if code:
        raise RiverError(f"the install on {target} failed: {out[-800:]}")
    return out


def _snapshot(path):
    """A consistent copy of a queue file (sqlite backup: no half-written page), as bytes."""
    with tempfile.TemporaryDirectory() as d:
        dst = Path(d) / "q.db"
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            out = sqlite3.connect(dst)
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()
        return dst.read_bytes()


def moved(conn):
    """{target, url, at} when this queue moved to a host, else None."""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (MOVED_KEY,)).fetchone()
    return json.loads(row[0]) if row else None


def moved_refusal(conn):
    """The error of every command on a queue that moved, or None."""
    m = moved(conn)
    if not m:
        return None
    where = f"open {m['url']}, or " if m.get("url") else ""
    return (f"this queue moved to {m['target']} on {m['at'][:10]}: {where}run maxpm there (ssh {m['target']}). "
            f"To use this copy here again: maxpm host forget")


def forget(conn):
    with core.tx(conn):
        n = conn.execute("DELETE FROM meta WHERE key=?", (MOVED_KEY,)).rowcount
    return {"forgot": bool(n)}


def move(conn, target):
    """This queue (and its archive) to the host, whose queue must have no projects; then mark this one moved."""
    projects, err = _json(target, "project", "list", timeout=60)
    if not isinstance(projects, list):
        raise RiverError(f"maxpm project list on {target} failed: {err[-400:]}")
    if projects:
        names = ", ".join(p.get("name", "?") for p in projects[:5])
        raise RiverError(f"the queue on {target} has projects already ({names}); MaximizePM does not merge two "
                         f"queues. Move to a host with a new queue")
    from . import space
    local = core.db_path()
    files = [("", local)]
    archive = space.archive_path(conn)
    if archive.exists():
        files.append(("-archive", archive))
    sent = []
    for suffix, path in files:
        # The host's queue file: maxpm db there; the archive beside it as <stem>-archive<suffix> (space.archive_path).
        script = ('set -e; db=$(' + REMOTE_MAXPM + ' db path); case "$db" in *.db) f="${db%.db}' + suffix + '.db";; '
                  '*) f="$db' + suffix + '";; esac; cat > "$f.incoming"; rm -f "$f-wal" "$f-shm"; '
                  'mv -f "$f.incoming" "$f"; echo "$f"')
        code, out = _ssh(target, script, _snapshot(path), timeout=600)
        if code:
            raise RiverError(f"the copy of {path.name} to {target} failed: {out[-400:]}")
        sent.append(out.splitlines()[-1] if out else path.name)
    _maxpm(target, "serve", "--kick", timeout=60)  # a serve that runs there reads the new file; none: no harm
    with core.tx(conn):
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                     (MOVED_KEY, json.dumps({"target": target, "url": None, "at": core.iso(core.now())})))
    paths = [dict(name=r[0], path=r[1]) for r in
             conn.execute("SELECT name, path FROM projects WHERE archived=0 AND path IS NOT NULL ORDER BY rank")]
    return {"files": sent, "projects": paths}


def setup(conn, target, do_move=False, reinstall=False):
    """The steps of maxpm host setup (module docstring). Returns what each step did and the page address."""
    _check_target(target)
    if moved(conn) and do_move:
        raise RiverError(moved_refusal(conn))
    steps = [f"{target} answers over SSH: {check(target)}"]
    have, want = host_version(target), f"maxpm {__version__}"
    if reinstall or have != want:
        install(target)
        steps.append(f"installed {want} from this computer's code" + (f" (it had {have})" if have else ""))
    else:
        steps.append(f"{have} is there already")
    res = {"target": target, "steps": steps}
    if do_move:
        res["moved"] = move(conn, target)
        steps.append(f"moved this queue to {target}: {', '.join(res['moved']['files'])}")
    code, out = _maxpm(target, "serve", "--install-login", timeout=120)
    if code:
        raise RiverError(f"maxpm serve --install-login on {target} failed: {out[-600:]}")
    steps.append(out.splitlines()[-1] if out else "maxpm serve starts at login there")
    res["serve"] = out
    phone, err = _json(target, "phone", "--on", timeout=120)
    if not isinstance(phone, dict):
        hint = (f" Tailscale on Linux changes its settings only for root or its operator: run once on the host "
                f"sudo tailscale set --operator=$USER, then maxpm host setup {target} again"
                if "operator" in err or "Access denied" in err or "permission" in err.lower() else "")
        res["phone_error"] = err[-600:] + hint
    else:
        res["phone"] = {k: phone.get(k) for k in ("url", "serving", "enable", "installed", "running")}
        if phone.get("url") and do_move:
            with core.tx(conn):
                m = moved(conn)
                m["url"] = phone["url"]
                conn.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(m), MOVED_KEY))
    return res


def render(res):
    """The text of maxpm host setup."""
    from . import tailnet
    lines = [f"MaximizePM on {res['target']}:"] + [f"  - {s}" for s in res["steps"]]
    if res.get("moved"):
        lines += ["", "This computer's queue is marked moved: its maxpm now refuses commands and names the host. "
                  "Stop the maxpm serve that runs here (quit the app, or: maxpm serve --remove-login, then stop it)."]
        if res["moved"]["projects"]:
            lines += ["Project folders do not move. On the host, clone each project and link its folder:"]
            lines += [f"  maxpm project path {p['name']} <folder>   (here: {p['path']})" for p in res["moved"]["projects"]]
    p = res.get("phone") or {}
    if res.get("phone_error"):
        lines += ["", f"The page over Tailscale is not on yet: {res['phone_error']}"]
    elif not p.get("installed") or not p.get("running"):
        lines += ["", f"Tailscale is not running on {res['target']}: install it there and sign in "
                  f"(tailscale.com/download), then run maxpm host setup {res['target']} again."]
    elif p.get("enable"):
        lines += ["", f"Tailscale waits for one approval before it serves the page: {p['enable'].get('url') or ''}",
                  f"Then run maxpm host setup {res['target']} again."]
    if p.get("url"):
        lines += ["", f"The page: {p['url']}   (on your laptop and phone, with Tailscale on, the same account)",
                  tailnet.qr_text(p["url"])]
    return "\n".join(lines)
