"""Secrets that sessions save and pass on by name (#5757): maxpm secret put|pipe|env|list|delete|log.

A vendor page shows a token once (a Google client secret, a Cloudflare API token). A session saves it under a name,
and a later step of any session gives it to a target by that name: a command's stdin (wrangler secret put, gh secret
set) or a line of a .env file. The value never goes to stdout, a reply, the queue's database, or a model's context.

Where the values are: on macOS the login Keychain (service "MaximizePM", account = the name; through
/usr/bin/security, so the Keychain's own rule lets that one program read them without a dialog). Elsewhere a folder
beside the queue, secrets/, one file a name, mode 0600. The queue's database keeps only the names (table secrets) and
who saved and used each one when (table secret_log).

An agent's sandbox refuses the Keychain: the command then goes through maxpm serve (op "secret",
routes/secrets.py), which runs outside it."""

import base64
import os
import re
import subprocess
import sys
from pathlib import Path

from . import core
from .core import RiverError

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
SECURITY = "/usr/bin/security"
SERVICE = "MaximizePM"
MAX_BYTES = 64 * 1024  # a token, a key file, a certificate; not a folder of files
BACKEND = None  # tests: an object with put(conn, name, data), get(conn, name), delete(conn, name); None: the platform's


def check_name(name):
    if not NAME_RE.match(name or ""):
        raise RiverError(f"a secret's name is letters, digits, '.', '_' and '-' (at most 100), starting with a letter "
                         f"or digit, not {name!r}: for example cloudflare-api-token")
    return name


def _queue_file(conn):
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row["file"] or core.db_path())


class Keychain:
    """The macOS login Keychain, through /usr/bin/security. The value goes in on stdin (security -i, as hex), never
    on a command line that ps shows. It is stored as 'b64:' + base64, so that -w prints it back exactly (a PEM key's
    newlines, a certificate's bytes)."""
    name = "keychain"

    def service(self, conn):
        f = _queue_file(conn).expanduser().resolve()
        # A test queue has its own service. No quote: security -i reads the name between quotes.
        return SERVICE if f == core.HOME_DB.expanduser().resolve() else f"{SERVICE} {f}".replace('"', "")

    def _run(self, args, stdin=None):
        try:
            return subprocess.run([SECURITY, *args], input=stdin, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RiverError(f"the macOS Keychain did not answer ({e.__class__.__name__})")

    def put(self, conn, name, data):
        word = ("b64:" + base64.b64encode(data).decode()).encode().hex()
        svc = self.service(conn)
        line = f'add-generic-password -U -s "{svc}" -a "{name}" -l "{svc}: {name}" -X {word}\n'
        r = self._run(["-i"], line.encode())  # security -i says 0 also on a failure: read it back
        if self.get(conn, name) != data:
            # Its words only: a line that repeats the command would hold the value as hex.
            said = re.sub(r"[0-9a-fA-F]{16,}", "...", r.stderr.decode(errors="replace")).strip()[:200]
            raise RiverError("the macOS Keychain refused the secret: nothing was saved. The login keychain may be "
                             "locked (unlock it in Keychain Access)" + (f"; security said: {said}" if said else ""))

    def get(self, conn, name):
        r = self._run(["find-generic-password", "-s", self.service(conn), "-a", name, "-w"])
        if r.returncode:
            return None
        out = r.stdout.decode().rstrip("\n")
        if out.startswith("b64:"):
            return base64.b64decode(out[4:])
        return out.encode()  # put there by hand (Keychain Access): its text as it is

    def delete(self, conn, name):
        self._run(["-i"], f'delete-generic-password -s "{self.service(conn)}" -a "{name}"\n'.encode())


class Files:
    """A folder beside the queue (Linux, WSL 2): secrets/<name>, the folder 0700, each file 0600."""
    name = "files"

    def folder(self, conn):
        return _queue_file(conn).expanduser().resolve().parent / "secrets"

    def put(self, conn, name, data):
        d = self.folder(conn)
        d.mkdir(mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        f = d / name
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.chmod(f, 0o600)

    def get(self, conn, name):
        f = self.folder(conn) / name
        return f.read_bytes() if f.is_file() else None

    def delete(self, conn, name):
        (self.folder(conn) / name).unlink(missing_ok=True)


def backend():
    if BACKEND is not None:
        return BACKEND
    return Keychain() if sys.platform == "darwin" else Files()


def needs_server():
    """Whether this command must ask maxpm serve: the Keychain, from inside an agent's sandbox."""
    from .cli import in_sandbox
    return BACKEND is None and sys.platform == "darwin" and in_sandbox()


def clean(data):
    """The value as typed or piped: one line loses its line end (echo adds one); text of several lines (a PEM key)
    and bytes stay as they are."""
    if not data:
        raise RiverError("the secret is empty: nothing was saved")
    if len(data) > MAX_BYTES:
        raise RiverError(f"a secret is at most {MAX_BYTES // 1024} KB; this one is {len(data) // 1024} KB")
    one = data[:-2] if data.endswith(b"\r\n") else data[:-1] if data.endswith(b"\n") else data
    return one if b"\n" not in one and one else data


def _log(conn, name, actor, action, detail=None):
    held = core._held_item(conn, actor)
    conn.execute("INSERT INTO secret_log (name, at, actor, action, detail, item_id) VALUES (?,?,?,?,?,?)",
                 (name, core.iso(core.now()), actor or "a person", action, detail, held["id"] if held else None))


def put(conn, name, data, actor=None, project=None, note=None, how="stdin"):
    """Save a value under a name (a new value replaces the old one). Replies with the name, never the value."""
    check_name(name)
    data = clean(data)
    pid = core._project(conn, project)["id"] if project else None
    backend().put(conn, name, data)
    at = core.iso(core.now())
    with core.tx(conn):
        old = conn.execute("SELECT name FROM secrets WHERE name=?", (name,)).fetchone()
        if old:
            conn.execute("UPDATE secrets SET updated_at=?, updated_by=?, project_id=COALESCE(?, project_id), "
                         "note=COALESCE(?, note) WHERE name=?", (at, actor, pid, note, name))
        else:
            conn.execute("INSERT INTO secrets (name, project_id, note, created_by, created_at, updated_by, updated_at,"
                         " uses) VALUES (?,?,?,?,?,?,?,0)", (name, pid, note, actor, at, actor, at))
        _log(conn, name, actor, "replace" if old else "put", how)
    return {"secret": name, "saved": True, "replaced": bool(old), "bytes": len(data), "store": backend().name}


def get(conn, name, actor=None, use=None):
    """The value, for a use that the log names (pipe: the command; env: the file). Only the CLI's pipe and env call
    it; their replies carry the name, never the value."""
    check_name(name)
    if not conn.execute("SELECT 1 FROM secrets WHERE name=?", (name,)).fetchone():
        raise RiverError(f"no secret {name}: maxpm secret list shows the names; a person saves one with "
                         f"maxpm secret put {name} (it asks for the value)")
    data = backend().get(conn, name)
    if data is None:
        raise RiverError(f"the secret {name} is listed but its value is gone from the {backend().name} store: save it "
                         f"again with maxpm secret put {name}")
    with core.tx(conn):
        conn.execute("UPDATE secrets SET used_at=?, used_by=?, uses=uses+1 WHERE name=?",
                     (core.iso(core.now()), actor, name))
        _log(conn, name, actor, "use", use)
    return data


def delete(conn, name, actor=None):
    check_name(name)
    if not conn.execute("SELECT 1 FROM secrets WHERE name=?", (name,)).fetchone():
        raise RiverError(f"no secret {name}: maxpm secret list shows the names")
    backend().delete(conn, name)
    with core.tx(conn):
        conn.execute("DELETE FROM secrets WHERE name=?", (name,))
        _log(conn, name, actor, "delete")
    return {"secret": name, "deleted": True}


def listing(conn, project=None):
    q = ("SELECT s.name, p.name AS project, s.note, s.created_by, s.created_at, s.updated_by, s.updated_at, s.used_at, s.used_by, "
         "s.uses FROM secrets s LEFT JOIN projects p ON p.id=s.project_id")
    args = []
    if project:
        q += " WHERE s.project_id=?"
        args.append(core._project(conn, project)["id"])
    return {"secrets": [dict(r) for r in conn.execute(q + " ORDER BY s.name", args)], "store": backend().name}


def log(conn, name=None, limit=30):
    q, args = "SELECT name, at, actor, action, detail, item_id FROM secret_log", []
    if name:
        q += " WHERE name=?"
        args.append(check_name(name))
    rows = [dict(r) for r in conn.execute(q + " ORDER BY id DESC LIMIT ?", args + [int(limit)])]
    return {"log": rows}


def set_env_line(path, key, data):
    """Put KEY=value in a .env file: replace the line of that key, or add it at the end. The file keeps its other
    lines, and a new file is mode 0600."""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        raise RiverError(f"--key is a variable name (letters, digits, _), not {key!r}")
    text = data.decode()
    if "\n" in text:
        raise RiverError("a .env line holds one line: this secret has several (a key file); pipe it to a command")
    if re.search(r"[\s#'\"$\\`]", text):
        text = "'" + text.replace("'", "'\\''") + "'"
    p = Path(path).expanduser()
    lines = p.read_text().splitlines() if p.exists() else []
    pat = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=")
    new, done = [], False
    for ln in lines:
        if pat.match(ln) and not done:
            new.append(f"{key}={text}")
            done = True
        elif not pat.match(ln):
            new.append(ln)
    if not done:
        new.append(f"{key}={text}")
    if not p.exists():
        os.close(os.open(p, os.O_WRONLY | os.O_CREAT, 0o600))
    p.write_text("\n".join(new) + "\n")
    return {"replaced": done}
