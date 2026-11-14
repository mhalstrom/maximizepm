"""Secrets that sessions save and pass on by name (#5757): maxpm secret put|pipe|env|list|delete|log.

A vendor page shows a token once (a Google client secret, a Cloudflare API token). A session saves it under a name,
and a later step of any session gives it to a target by that name: a command's stdin (wrangler secret put, gh secret
set) or a line of a .env file. The value never goes to stdout, a reply, the queue's database, or a model's context.

Where the values are: on macOS the login Keychain (service "MaximizePM", account = the name; through
/usr/bin/security, so the Keychain's own rule lets that one program read them without a dialog). Elsewhere a folder
beside the queue, secrets/, one file a name, mode 0600. The queue's database keeps only the names (table secrets) and
who saved and used each one when (table secret_log).

An agent's sandbox refuses the Keychain: the command then goes through maxpm serve (op "secret",
routes/secrets.py), which runs outside it.

Sets and grants (#5822): a person puts secrets into named sets (maxpm secret set) and grants a set to a project, a
goal, a kind of task, one task or one agent (maxpm secret grant), until a revoke or an end date; a goal grant ends
when the goal is complete, a task grant when the task closes. A session may use, replace or delete a secret only when
a grant of one of its sets covers the session (allowed). A secret in no set: only the session that saved it last. A
person needs no grant. Who asks comes from session_of, the one place that the session check (#5823) replaces."""

import base64
import os
import re
import subprocess
import sys
import threading
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


def put(conn, name, data, actor=None, project=None, note=None, how="stdin", var=None):
    """Save a value under a name (a new value replaces the old one). Replies with the name, never the value."""
    check_name(name)
    data = clean(data)
    var = check_var(var) if var else None
    pid = core._project(conn, project)["id"] if project else None
    if conn.execute("SELECT 1 FROM secrets WHERE name=?", (name,)).fetchone():
        _require(conn, name, actor, "replace")  # a new value in a set changes what every grant of the set gives
    backend().put(conn, name, data)
    at = core.iso(core.now())
    with core.tx(conn):
        old = conn.execute("SELECT name FROM secrets WHERE name=?", (name,)).fetchone()
        if old:
            conn.execute("UPDATE secrets SET updated_at=?, updated_by=?, project_id=COALESCE(?, project_id), "
                         "note=COALESCE(?, note), var=COALESCE(?, var) WHERE name=?", (at, actor, pid, note, var, name))
        else:
            conn.execute("INSERT INTO secrets (name, project_id, note, created_by, created_at, updated_by, updated_at,"
                         " uses, var) VALUES (?,?,?,?,?,?,?,0,?)", (name, pid, note, actor, at, actor, at, var))
        _log(conn, name, actor, "replace" if old else "put", how)
        level = conn.execute("SELECT level FROM secrets WHERE name=?", (name,)).fetchone()["level"]
    return {"secret": name, "saved": True, "replaced": bool(old), "bytes": len(data), "store": backend().name,
            "level": level}


def get(conn, name, actor=None, use=None, how=None, argv=None):
    """The value, for a use that the log names (pipe or run: the command; env: the file). Only the CLI's pipe, run
    and env call it; their replies carry the name, never the value. how (pipe, run, env) and argv: the use that the
    secret's protection level checks (check_level)."""
    check_name(name)
    row = conn.execute("SELECT * FROM secrets WHERE name=?", (name,)).fetchone()
    if not row:
        raise RiverError(f"no secret {name}: maxpm secret list shows the names; a person saves one with "
                         f"maxpm secret put {name} (it asks for the value)")
    ok = _require(conn, name, actor, "use", use)
    if how and row["kind"] == "cookies":
        raise RiverError(f"{name} holds saved sign-in cookies: maxpm serve loads them into a granted session's "
                         f"browser; no command gets them")
    if how:
        why = check_level(row, how, argv or [])
        if why:
            with core.tx(conn):
                _log(conn, name, actor, "refused", f"{use or how}: {why}")
            held = core._held_item(conn, actor)
            raise RiverError(f"{why}. Ask the manager: " + _ask_line(
                conn, f"Allow {how} of the secret {name}"
                + (f" for {' '.join(argv[:2])}" if argv else "") + ": " + level_hint(name, how, argv or []),
                held["id"] if held else None))
    data = backend().get(conn, name)
    if data is None:
        raise RiverError(f"the secret {name} is listed but its value is gone from the {backend().name} store: save it "
                         f"again with maxpm secret put {name}")
    with core.tx(conn):
        conn.execute("UPDATE secrets SET used_at=?, used_by=?, uses=uses+1 WHERE name=?",
                     (core.iso(core.now()), actor, name))
        _log(conn, name, actor, "use", "; ".join(x for x in (use, ok["by"]) if x))
    return data


def delete(conn, name, actor=None):
    check_name(name)
    if not conn.execute("SELECT 1 FROM secrets WHERE name=?", (name,)).fetchone():
        raise RiverError(f"no secret {name}: maxpm secret list shows the names")
    _require(conn, name, actor, "delete")
    backend().delete(conn, name)
    backend().delete(conn, name + TOTP_SUFFIX)  # a sign-in's authenticator key; nothing for a token
    with core.tx(conn):
        conn.execute("DELETE FROM secrets WHERE name=?", (name,))
        _log(conn, name, actor, "delete")
    return {"secret": name, "deleted": True}


def listing(conn, project=None):
    q = ("SELECT s.name, p.name AS project, s.note, s.created_by, s.created_at, s.updated_by, s.updated_at, s.used_at, s.used_by, "
         "s.uses, s.level, s.allow_commands, s.allow_hosts, s.allow_env, s.var, s.kind, s.site, s.account, "
         "s.two_step, s.fail_why "
         "FROM secrets s LEFT JOIN projects p ON p.id=s.project_id")
    args = []
    if project:
        q += " WHERE s.project_id=?"
        args.append(core._project(conn, project)["id"])
    rows = [dict(r) for r in conn.execute(q + " ORDER BY s.name", args)]
    member = {}
    for r in conn.execute("SELECT m.name, s.name AS set_name FROM secret_set_members m JOIN secret_sets s "
                          "ON s.id=m.set_id ORDER BY s.name"):
        member.setdefault(r["name"], []).append(r["set_name"])
    for r in rows:
        r["sets"] = member.get(r["name"], [])
    return {"secrets": rows, "store": backend().name}


def log(conn, name=None, limit=30, actor=None):
    q, args, where = "SELECT name, at, actor, action, detail, item_id FROM secret_log", [], []
    if name:
        where.append("name=?")
        args.append(check_name(name))
    if actor:  # #5834: one session's uses (maxpm secret check)
        where.append("actor=?")
        args.append(actor)
    q += (" WHERE " + " AND ".join(where)) if where else ""
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


# ---- protection levels (#5825)

# locked: only the commands that the person named; hidden (the default): any command; open: as before #5825.
# Locked and hidden: maxpm secret pipe and run mask the value in the command's output, and env needs allow_env.
LEVELS = ("locked", "hidden", "open")
MASK = b"***"
MASK_MIN = 4  # a shorter value is masked too, but only as itself: its encodings would match ordinary text


def check_var(var):
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", var or ""):
        raise RiverError(f"a variable name is letters, digits and _, not starting with a digit, not {var!r}: "
                         f"for example GH_TOKEN")
    return var


def _lines(text):
    return [x.strip() for x in (text or "").splitlines() if x.strip()]


def _command_allowed(entries, argv):
    """Whether argv starts with one of the entries: each is the first words of a command (wrangler, npx wrangler),
    the first one compared by its file name, so /opt/homebrew/bin/wrangler is wrangler."""
    words = [os.path.basename(argv[0])] + list(argv[1:]) if argv else []
    for e in entries:
        want = e.split()
        if want and words[:len(want)] == want:
            return True
    return False


def check_level(row, how, argv):
    """Why the secret's level refuses this use (how: pipe, run, env), or None."""
    level = row["level"] or "hidden"
    if level == "open":
        return None
    if how == "env":
        if not row["allow_env"]:
            return f"the secret {row['name']} is {level}: writing it into a file is off for it"
        return None
    if level == "locked" and not _command_allowed(_lines(row["allow_commands"]), argv):
        named = ", ".join(_lines(row["allow_commands"])) or "none yet"
        return (f"the secret {row['name']} is locked to the commands {named}; "
                f"{os.path.basename(argv[0]) if argv else 'this command'} is not one of them")
    return None


def level_hint(name, how, argv):
    """The command a person runs to allow what check_level refused."""
    if how == "env":
        return f"maxpm secret level {name} --env on"
    return f"maxpm secret level {name} --command {os.path.basename(argv[0]) if argv else '<command>'}"


def protection(conn, name):
    """What the CLI needs to give the value: the level (masked or not) and the variable for run."""
    r = conn.execute("SELECT level, var FROM secrets WHERE name=?", (name,)).fetchone()
    return {"level": r["level"] if r else "hidden", "var": r["var"] if r else None}


def set_level(conn, name, level=None, add_commands=(), remove_commands=(), add_hosts=(), remove_hosts=(),
              env=None, var=None, actor=None):
    """Change a secret's level, its allowed commands and hosts, whether env may write it, and its variable. Only a
    person."""
    check_name(name)
    if not session_of(conn, actor)["person"]:
        raise RiverError("only a person changes a secret's protection. Ask the manager to bring it to the person: "
                         + _ask_line(conn, f"Please change the protection of the secret {name}: <what, why>"))
    r = conn.execute("SELECT * FROM secrets WHERE name=?", (name,)).fetchone()
    if not r:
        raise RiverError(f"no secret {name}: maxpm secret list shows the names")
    if level is not None and level not in LEVELS:
        raise RiverError(f"a level is one of {', '.join(LEVELS)}, not {level!r}")
    cmds = [c for c in _lines(r["allow_commands"]) if c not in [" ".join(x.split()) for x in remove_commands]]
    cmds += [" ".join(c.split()) for c in add_commands if c.strip() and " ".join(c.split()) not in cmds]
    hosts = [h for h in _lines(r["allow_hosts"]) if h not in [x.strip().lower() for x in remove_hosts]]
    hosts += [h.strip().lower() for h in add_hosts if h.strip() and h.strip().lower() not in hosts]
    new = {"level": level or r["level"], "allow_commands": "\n".join(cmds) or None,
           "allow_hosts": "\n".join(hosts) or None,
           "allow_env": int(env) if env is not None else r["allow_env"],
           "var": check_var(var) if var else r["var"]}
    with core.tx(conn):
        conn.execute("UPDATE secrets SET level=?, allow_commands=?, allow_hosts=?, allow_env=?, var=? WHERE name=?",
                     (new["level"], new["allow_commands"], new["allow_hosts"], new["allow_env"], new["var"], name))
        changed = [k for k in new if new[k] != r[k]]
        if changed:
            _log(conn, name, actor, "level", "; ".join(f"{k} {new[k]!r}" for k in changed))
    return {"secret": name, "level": new["level"], "commands": cmds, "hosts": hosts, "env": bool(new["allow_env"]),
            "var": new["var"], "changed": changed}


def mask_forms(data):
    """The byte strings that show the value in output: itself, its base64 (standard and URL-safe, with no '='), its
    hex (lower and upper case), and for a value of several lines each long line. Longest first, so that a longer
    form is masked before a shorter one inside it."""
    if not data:
        return []
    forms = {data}
    if len(data) >= MASK_MIN:
        b = base64.b64encode(data).rstrip(b"=")
        forms |= {b, base64.urlsafe_b64encode(data).rstrip(b"="), data.hex().encode(), data.hex().upper().encode()}
        forms |= {ln.strip() for ln in data.splitlines() if len(ln.strip()) >= 16}
    return sorted(forms, key=len, reverse=True)


class Masker:
    """Masks the value's forms in a stream of chunks. It holds back the last (longest form - 1) bytes until more
    comes, so a form that two chunks split is still masked; flush() gives the rest at the end."""

    def __init__(self, data):
        self.forms = mask_forms(data)
        self.keep = max((len(f) for f in self.forms), default=1) - 1
        self.buf = b""

    def _mask(self, b):
        for f in self.forms:
            b = b.replace(f, MASK)
        return b

    def feed(self, chunk):
        self.buf = self._mask(self.buf + chunk)
        if len(self.buf) <= self.keep:
            return b""
        cut = len(self.buf) - self.keep
        out, self.buf = self.buf[:cut], self.buf[cut:]
        return out

    def flush(self):
        out, self.buf = self._mask(self.buf), b""
        return out


# ---- sign-ins (#5826): a site, an account and a password that maxpm serve fills in a session's browser itself

TOTP_SUFFIX = "#totp"  # the store name of a sign-in's authenticator key: '<name>#totp' (no secret name has a '#')
TWO_STEP = ("person", "agents")


def norm_site(site):
    """A site as a sign-in keeps it: lower case, no scheme, path, port or www. (accounts.google.com stays)."""
    s = re.sub(r"^[a-z]+://", "", (site or "").strip().lower()).split("/")[0].split(":")[0].removeprefix("www.")
    if not re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", s) and s != "localhost":
        raise RiverError(f"a site is a host name such as github.com, not {site!r}")
    return s


def totp_key(text):
    """The authenticator key as base32 bytes check: the key that a site shows (spaces allowed) or its otpauth://
    address. Raises when it is not one."""
    t = text.decode(errors="replace").strip() if isinstance(text, bytes) else str(text).strip()
    if t.startswith("otpauth://"):
        from urllib.parse import parse_qs, urlsplit
        t = (parse_qs(urlsplit(t).query).get("secret") or [""])[0]
    t = re.sub(r"[\s-]", "", t).upper()
    try:
        if not t or not base64.b32decode(t + "=" * (-len(t) % 8)):
            raise ValueError
    except (ValueError, base64.binascii.Error):
        raise RiverError("the authenticator key is the letters and digits a site shows under 'can't scan the code' "
                         "(base32), or its otpauth:// address: nothing was saved")
    return t.encode()


def totp_code(key, at=None, digits=6, step=30):
    """The 6-digit code of an authenticator key now (RFC 6238, SHA-1, 30 s): what an authenticator app shows."""
    import hashlib
    import hmac
    import time
    t = key.decode() if isinstance(key, bytes) else key
    k = base64.b32decode(t + "=" * (-len(t) % 8))
    h = hmac.new(k, int((time.time() if at is None else at) // step).to_bytes(8, "big"), hashlib.sha1).digest()
    o = h[-1] & 15
    return str((int.from_bytes(h[o:o + 4], "big") & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


def signin_save(conn, name, site=None, account=None, password=None, totp=None, two_step=None, reset=False,
                actor=None, project=None, note=None):
    """Save or change a sign-in: its site and account, its password (a new value replaces the old one), its
    authenticator key, who passes a 2-step check, and reset (fill again after a failed try). two_step agents and
    reset: a person only. Replies with names, never a value."""
    check_name(name)
    row = conn.execute("SELECT * FROM secrets WHERE name=?", (name,)).fetchone()
    if row and row["kind"] != "signin":
        raise RiverError(f"the secret {name} is a token, not a sign-in: give the sign-in another name")
    if not row and not (site and account and password):
        raise RiverError("a new sign-in needs --site, --account and the password: maxpm secret signin <name> "
                         "--site <site> --account <account> (it asks for the password)")
    if (two_step == "agents" or reset) and not session_of(conn, actor)["person"]:
        raise RiverError("only a person lets agents pass a 2-step check or resets a failed sign-in. Ask the manager: "
                         + _ask_line(conn, f"Please check the sign-in {name}: <what>"))
    if two_step is not None and two_step not in TWO_STEP:
        raise RiverError(f"--two-step is person or agents, not {two_step!r}")
    site = norm_site(site) if site else None
    if account is not None and (not account.strip() or re.search(r"\s", account)):
        raise RiverError("an account is a name or an address with no spaces")
    key = totp_key(totp) if totp else None
    if row:
        _require(conn, name, actor, "replace")
    if password:
        put(conn, name, password, actor, project, note, "signin")
    if key:
        backend().put(conn, name + TOTP_SUFFIX, key)
    with core.tx(conn):
        conn.execute("UPDATE secrets SET kind='signin', site=COALESCE(?, site), account=COALESCE(?, account), "
                     "two_step=COALESCE(?, two_step), note=COALESCE(?, note) WHERE name=?",
                     (site, account, two_step, note, name))
        if reset or password:
            conn.execute("UPDATE secrets SET fail_at=NULL, fail_why=NULL WHERE name=?", (name,))
        if key:
            _log(conn, name, actor, "put", "authenticator key")
        if reset:
            _log(conn, name, actor, "reset", "a person reset the failed sign-in")
        r = conn.execute("SELECT * FROM secrets WHERE name=?", (name,)).fetchone()
    return {"secret": name, "site": r["site"], "account": r["account"], "two_step": r["two_step"],
            "totp": backend().get(conn, name + TOTP_SUFFIX) is not None, "made": not row,
            "failed": r["fail_why"], "level": r["level"]}


def signins_for(conn, host):
    """The sign-ins whose site is host or a site above it (google.com for accounts.google.com), longest site first."""
    host = (host or "").lower().removeprefix("www.")
    rows = conn.execute("SELECT * FROM secrets WHERE kind='signin' AND site IS NOT NULL").fetchall()
    return sorted([r for r in rows if host == r["site"] or host.endswith("." + r["site"])],
                  key=lambda r: (-len(r["site"]), r["name"]))


def signin_failed(conn, name, why):
    with core.tx(conn):
        conn.execute("UPDATE secrets SET fail_at=?, fail_why=? WHERE name=?", (core.iso(core.now()), why, name))
        _log(conn, name, "maxpm", "signin-failed", why)


# ---- saved browser sign-ins (#5827): the cookies of one site, as a secret of kind 'cookies' in a set

def pack_cookies(cookies):
    """The cookies of one site as the store keeps them: JSON, zlib, so a site with many cookies fits MAX_BYTES."""
    import json
    import zlib
    data = zlib.compress(json.dumps(cookies, separators=(",", ":")).encode(), 9)
    if len(data) > MAX_BYTES:
        raise RiverError(f"the sign-in cookies of this site are {len(data) // 1024} KB packed, more than "
                         f"{MAX_BYTES // 1024} KB: nothing was saved")
    return data


def unpack_cookies(data):
    import json
    import zlib
    return json.loads(zlib.decompress(data).decode()) if data else []


def cookies_save(conn, name, site, cookies, actor=None, set_name=None, by_serve=False):
    """Save the cookies of one site under name (a new save replaces them), and put it in set_name. A person saves
    one (by_serve: serve saves a granted one back after a new sign-in, #5827). Replies with names and a count."""
    check_name(name)
    site = norm_site(site)
    row = conn.execute("SELECT kind FROM secrets WHERE name=?", (name,)).fetchone()
    if row and row["kind"] != "cookies":
        raise RiverError(f"the secret {name} is a {row['kind']}, not saved sign-in cookies: give them another name")
    if not by_serve and not session_of(conn, actor)["person"]:
        raise RiverError("only a person saves a browser's sign-in into a set. Ask the manager: "
                         + _ask_line(conn, f"Please save the sign-in of {site} into a set"))
    data = pack_cookies(cookies)  # bytes as they are: put()'s clean() would cut a last line end of the packed data
    who = "maxpm" if by_serve else actor
    backend().put(conn, name, data)
    at = core.iso(core.now())
    with core.tx(conn):
        if row:
            conn.execute("UPDATE secrets SET updated_at=?, updated_by=?, site=? WHERE name=?", (at, who, site, name))
        else:
            conn.execute("INSERT INTO secrets (name, created_by, created_at, updated_by, updated_at, uses, kind, site) "
                         "VALUES (?,?,?,?,?,0,'cookies',?)", (name, who, at, who, at, site))
        _log(conn, name, who, "replace" if row else "put",
             f"browser sign-in {'saved back' if by_serve else 'saved'} ({len(cookies)} cookies of {site})")
    if set_name:
        set_change(conn, set_name, add=[name], actor=actor)
    return {"secret": name, "site": site, "cookies": len(cookies), "set": set_name}


def cookie_sites(conn):
    """The saved sign-ins: [(name, site)]."""
    return [(r["name"], r["site"]) for r in conn.execute(
        "SELECT name, site FROM secrets WHERE kind='cookies' AND site IS NOT NULL ORDER BY name")]


# ---- sets and grants (#5822)

GRANT_SCOPES = ("project", "goal", "kind", "item", "agent")
# The kinds of task that a kind grant names: the task kinds of maxpm add, and the kinds that MaximizePM makes.
GRANT_KINDS = ("work", "operational", "review", "fixes", "deploy", "monitor")


IDENTITY = None  # tests: fn(conn, actor) -> {'name', 'person'} in place of the process tree
PEER = threading.local()  # serve: (client port, serve port, the pid the CLI says) of the request that runs now
# The programs of agent CLIs (the adapters' DETECT process, river/agents) and node, which runs pi, Gemini and the Codex
# wrapper. Claude Code's process has its version as its name (2.1.325): VERSION_NAME.
AGENT_PROGRAMS = {"claude", "codex", "copilot", "cursor-agent", "gemini", "goose", "grok", "hermes", "opencode", "amp",
                  "aider", "pi", "node"}
VERSION_NAME = re.compile(r"^\d+\.\d+\.\d+")


def _chain_now(conn):
    """The processes that run this request, from the kernel: in serve, those above the process that holds the
    request's connection (the CLI's pid first, then the recorded sessions': the Claude Code sandbox's proxy runs
    inside the session's own process); in the CLI, its own. None when the kernel does not tell."""
    from . import peer
    p = getattr(PEER, "value", None)
    if p is None:
        return peer.chain(os.getpid())
    if p[0] is None or p[1] is None:  # serve does not know the connection: no answer, never serve's own tree
        return None
    hints = [p[2]] + [r["pid"] for r in conn.execute(
        "SELECT pid FROM agents WHERE kind='ai' AND pid IS NOT NULL AND host=?", (core.this_host(),))]
    pid = peer.holder(p[0], p[1], hints)
    return peer.chain(pid) if pid else None


def session_of(conn, actor):
    """Who asks, for every grant check and grant change (#5823, measured in #5821): {'name', 'person', 'how'}.
    MaximizePM never trusts the name a command types. It reads the processes above the command from the kernel:
    - a recorded session's process (agents.pid, and its start time) is above it: it is that session; a --as name of
      another session or a person is refused;
    - an agent program is above it, of no recorded session: refused until the session's maxpm go records it;
    - neither: a person (no name, or a person's name); a session's name is refused.
    The kernel does not tell (a sandbox): refused, never the typed name. Limit: a command that leaves the tree on
    purpose (setsid, nohup in a shell that ends) has launchd above it and counts as a person; so does a session's
    command outside its sandbox that reads the Keychain itself (#5824)."""
    if IDENTITY is not None:
        return IDENTITY(conn, actor)
    ch = _chain_now(conn)
    if not ch:
        raise RiverError("MaximizePM cannot read which session runs this command (a sandbox hides its processes): "
                         "run it with maxpm serve running; the command goes through serve, which checks it")
    host = core.this_host()
    rows = {r["pid"]: r for r in conn.execute(
        "SELECT name, pid, pid_start FROM agents WHERE kind='ai' AND host=? AND pid IN (%s)" % ",".join("?" * len(ch)),
        [host] + [c[0] for c in ch])}
    for pid, _ppid, comm, start in ch:
        r = rows.get(pid)
        if r and (r["pid_start"] is None or r["pid_start"] == start):
            if actor and actor != r["name"]:
                raise RiverError(f"this command runs in the session {r['name']}, not {actor}: MaximizePM confirms the "
                                 f"session from its processes, not from --as. Run it as maxpm --as {r['name']} ...")
            return {"name": r["name"], "person": False, "how": f"process {pid} ({comm})"}
    agent = next((c for c in ch if c[2].lower() in AGENT_PROGRAMS or VERSION_NAME.match(c[2])), None)
    if agent:
        raise RiverError(f"this command runs under {agent[2]} (pid {agent[0]}), and MaximizePM has recorded no "
                         f"session for that process: run maxpm go in the session first (it records the session's "
                         f"process), then this command again")
    if actor and not core._is_person(conn, actor):
        raise RiverError(f"no process of the session {actor} runs this command: a session runs it itself; a person "
                         f"runs it with no --as, or --as <person>")
    return {"name": actor, "person": True, "how": "no agent program above it"}


def _ask_line(conn, what, item_id=None):
    """The command that asks for a grant: to the manager when one is active, else to a person."""
    to = core.active_manager(conn)
    if not to:
        r = conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY last_seen DESC LIMIT 1").fetchone()
        to = r["name"] if r else "<person>"
    return f'maxpm ask {to} "{what}"' + (f" --item {item_id}" if item_id else "") + " --blocked"


def _scopes_of(conn, name):
    """What covers a session now: its name, the items it holds, their kinds and projects, the goals of those items and
    the goals it owns (and their parent goals, so a goal grant covers its sub-goals), and the owned goals' projects."""
    held = conn.execute("SELECT id, project_id, kind FROM items WHERE assignee=? AND status IN ('in_progress','held')",
                        (name,)).fetchall()
    items = {r["id"] for r in held}
    goals = {r["goal_id"] for r in conn.execute(
        "SELECT ig.goal_id FROM item_goals ig JOIN items i ON i.id=ig.item_id "
        "WHERE i.assignee=? AND i.status IN ('in_progress','held')", (name,))}
    owned = conn.execute("SELECT id, project_id FROM goals WHERE owner=? AND status='open'", (name,)).fetchall()
    goals |= {r["id"] for r in owned}
    todo = list(goals)
    while todo:
        r = conn.execute("SELECT parent_id FROM goals WHERE id=?", (todo.pop(),)).fetchone()
        if r and r["parent_id"] and r["parent_id"] not in goals:
            goals.add(r["parent_id"])
            todo.append(r["parent_id"])
    return {"agent": {name}, "item": items, "kind": {r["kind"] for r in held}, "goal": goals,
            "project": {r["project_id"] for r in held} | {r["project_id"] for r in owned}}


def _grant_end(conn, g, at=None):
    """Why a grant no longer gives its set (revoked, its end date passed, its goal complete, its item closed), or
    None while it gives it."""
    if g["revoked_at"]:
        return f"revoked {core.show_time(g['revoked_at'])} by {g['revoked_by'] or 'a person'}"
    if g["until"] and g["until"] <= (at or core.iso(core.now())):
        return f"ended {core.show_time(g['until'])}"
    if g["scope"] == "goal":
        r = conn.execute("SELECT status FROM goals WHERE id=?", (int(g["value"]),)).fetchone()
        if not r or r["status"] != "open":
            return "its goal is complete"
    if g["scope"] == "item":
        r = conn.execute("SELECT status FROM items WHERE id=?", (int(g["value"]),)).fetchone()
        if not r or r["status"] in core.CLOSED_STATES:
            return "its task is closed"
    return None


def _covers(g, scopes):
    v = g["value"]
    return (int(v) if g["scope"] in ("project", "goal", "item") else v) in scopes[g["scope"]]


def _scope_text(conn, scope, value):
    if scope == "project":
        return f"project {core._project_name(conn, int(value))}"
    if scope == "goal":
        r = conn.execute("SELECT name FROM goals WHERE id=?", (int(value),)).fetchone()
        return f"goal {r['name'] if r else value}"
    if scope == "item":
        return f"task #{value}"
    return f"{scope} {value}"


def allowed(conn, name, actor, who=None):
    """Whether actor may use (or replace, or delete) the secret name: {'ok', 'by', 'sets', 'why'}. 'by' says what
    allowed it (a person, the saver, grant g<id>); 'why' says what is missing. who: the session as serve knows it
    itself (the owner of a browser that it fills, #5826); else session_of confirms it."""
    who = who or session_of(conn, actor)
    if who["person"]:
        return {"ok": True, "by": None, "sets": []}
    sets = conn.execute("SELECT s.id, s.name FROM secret_set_members m JOIN secret_sets s ON s.id=m.set_id "
                        "WHERE m.name=? ORDER BY s.name", (name,)).fetchall()
    if not sets:
        r = conn.execute("SELECT updated_by FROM secrets WHERE name=?", (name,)).fetchone()
        if r and r["updated_by"] == who["name"]:
            return {"ok": True, "by": "the session that saved it", "sets": []}
        return {"ok": False, "sets": [], "why": f"the secret {name} is in no set, and only the session that saved it "
                                                "uses a secret with no set"}
    scopes = _scopes_of(conn, who["name"])
    at = core.iso(core.now())
    marks = ",".join("?" * len(sets))
    for g in conn.execute(f"SELECT * FROM secret_grants WHERE set_id IN ({marks}) ORDER BY id", [s["id"] for s in sets]):
        if _covers(g, scopes) and not _grant_end(conn, g, at):
            set_name = next(s["name"] for s in sets if s["id"] == g["set_id"])
            return {"ok": True, "by": f"grant g{g['id']}: set {set_name} to {_scope_text(conn, g['scope'], g['value'])}",
                    "sets": [s["name"] for s in sets]}
    names = ", ".join(s["name"] for s in sets)
    return {"ok": False, "sets": [s["name"] for s in sets],
            "why": f"the secret {name} is in the set{'s' if len(sets) > 1 else ''} {names}, and no grant of "
                   f"{'them' if len(sets) > 1 else 'it'} covers {who['name']}"}


def _require(conn, name, actor, what, detail=None):
    """allowed(), or a refusal that the log keeps and that names the set and the request to the manager.
    what: use, replace or delete; detail: the use as the log names it (the command, the file)."""
    ok = allowed(conn, name, actor)
    if ok["ok"]:
        return ok
    who = session_of(conn, actor)["name"] or actor  # allowed() confirmed it: the session's own name
    held = core._held_item(conn, who)
    scope = f"--item {held['id']}" if held else f"--agent {who}"
    if ok["sets"]:
        grant_line = f"maxpm secret grant {ok['sets'][0]} {scope}"
    else:
        grant_line = f"maxpm secret set <set> --add {name}; maxpm secret grant <set> {scope}"
    to, sent = _refusal_alert(conn, name, who, ok, grant_line, held)
    with core.tx(conn):
        _log(conn, name, who, "refused", f"{detail or what}: {ok['why']}")
    raise RiverError(f"{ok['why']}: the {what} is refused. "
                     + (f"MaximizePM asked {to} for the grant ({grant_line}); " if sent else
                        f"MaximizePM asked {to} for the grant already; " if to else
                        "No manager or person is registered to ask; ")
                     + "take other work, or release the item until the grant comes")


REFUSED_AGAIN = 3600  # seconds: a refused use alerts once per secret and session in this time


def _refusal_alert(conn, name, who, ok, grant_line, held):
    """#5828: a refused use asks for the grant by itself: the manager when one of the secret's sets is marked 'the
    manager may grant' and a manager is active, else the person (a phone alert). Once per REFUSED_AGAIN. Returns
    (to, sent)."""
    marked = bool(ok["sets"]) and conn.execute(
        "SELECT 1 FROM secret_sets WHERE manager_may_grant=1 AND name IN (%s)" % ",".join("?" * len(ok["sets"])),
        ok["sets"]).fetchone()
    manager = core.active_manager(conn) if marked else None
    person = conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY last_seen DESC LIMIT 1").fetchone()
    to = manager or (person["name"] if person else None)
    if not to:
        return None, False
    since = core.iso(core.now() - __import__("datetime").timedelta(seconds=REFUSED_AGAIN))
    if conn.execute("SELECT 1 FROM secret_log WHERE name=? AND actor=? AND action='refused' AND at>?",
                    (name, who, since)).fetchone():
        return to, False
    body = (f"{who} was refused the secret {name}: {ok['why']}"
            + (f" (task #{held['id']})" if held else "") + f". To grant it: {grant_line}"
            + (" (the set is marked 'the manager may grant')" if manager else ""))
    with core.tx(conn):
        core._send(conn, "alert", "maxpm", body, to=to, item_id=held["id"] if held else None)
    return to, True


def _may_grant(conn, actor, s, what):
    """A person grants and revokes any set; the active manager only the sets that the person marked 'the manager
    may grant' (#5828). Returns who it is."""
    who = session_of(conn, actor)
    if who["person"]:
        return who
    if core._is_manager(conn, who["name"]) and s["manager_may_grant"]:
        return who
    if core._is_manager(conn, who["name"]):
        person = conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY last_seen DESC LIMIT 1").fetchone()
        raise RiverError(f"the person did not mark the set {s['name']} 'the manager may grant': only a person may "
                         f"{what} it. Ask: maxpm ask {person['name'] if person else '<person>'} \"Please {what} the "
                         f"set {s['name']}: <to what, why>\" --blocked")
    raise RiverError(f"only a person or the manager may {what} a credential set. Ask the manager: "
                     + _ask_line(conn, f"Please {what} the set {s['name']}: <to what, why>"))


def _person_only(conn, actor, what):
    """what: the step as a verb, for example 'change a credential set'."""
    if not session_of(conn, actor)["person"]:
        raise RiverError(f"only a person may {what}. Ask the manager to bring it to the person: "
                         + _ask_line(conn, f"Please {what}: <which set, to what, why>"))


def _set(conn, name):
    r = conn.execute("SELECT * FROM secret_sets WHERE name=?", (name,)).fetchone()
    if not r:
        known = [x["name"] for x in conn.execute("SELECT name FROM secret_sets ORDER BY name")]
        raise RiverError(f"no set {name!r}; " + (f"sets: {', '.join(known)}" if known else
                                                 f"make one: maxpm secret set {name} --add <secret>"))
    return r


def _set_info(conn, s):
    d = {k: s[k] for k in ("name", "note", "created_by", "created_at")}
    d["manager_may_grant"] = bool(s["manager_may_grant"])
    d["secrets"] = [r["name"] for r in conn.execute("SELECT name FROM secret_set_members WHERE set_id=? ORDER BY name",
                                                    (s["id"],))]
    at = core.iso(core.now())
    d["grants"] = sum(1 for g in conn.execute("SELECT * FROM secret_grants WHERE set_id=?", (s["id"],))
                      if not _grant_end(conn, g, at))
    return d


def set_change(conn, name, add=(), remove=(), note=None, manager_may_grant=None, delete=False, actor=None):
    """Make a set, or change it: add and remove secrets, its note, the mark 'the manager may grant'; or delete it
    with its grants. Only a person."""
    check_name(name)
    if re.match(r"^[gG]\d+$", name):
        raise RiverError(f"{name} looks like a grant id (maxpm secret revoke g12): give the set another name")
    _person_only(conn, actor, "change a credential set")
    for n in list(add) + list(remove):
        check_name(n)
    with core.tx(conn):
        s = conn.execute("SELECT * FROM secret_sets WHERE name=?", (name,)).fetchone()
        if delete:
            if not s:
                _set(conn, name)
            for r in conn.execute("SELECT name FROM secret_set_members WHERE set_id=?", (s["id"],)).fetchall():
                _log(conn, r["name"], actor, "set-remove", f"set {name} deleted")
            conn.execute("DELETE FROM secret_sets WHERE id=?", (s["id"],))
            return {"set": name, "deleted": True}
        made = not s
        if made:
            conn.execute("INSERT INTO secret_sets (name, note, manager_may_grant, created_by, created_at) "
                         "VALUES (?,?,?,?,?)", (name, note, int(bool(manager_may_grant)), actor, core.iso(core.now())))
            s = conn.execute("SELECT * FROM secret_sets WHERE name=?", (name,)).fetchone()
        else:
            if note is not None:
                conn.execute("UPDATE secret_sets SET note=? WHERE id=?", (note, s["id"]))
            if manager_may_grant is not None:
                conn.execute("UPDATE secret_sets SET manager_may_grant=? WHERE id=?", (int(manager_may_grant), s["id"]))
        for n in add:
            if not conn.execute("SELECT 1 FROM secrets WHERE name=?", (n,)).fetchone():
                raise RiverError(f"no secret {n}: maxpm secret list shows the names; save it first with "
                                 f"maxpm secret put {n}")
            if conn.execute("INSERT OR IGNORE INTO secret_set_members (set_id, name, added_by, added_at) "
                            "VALUES (?,?,?,?)", (s["id"], n, actor, core.iso(core.now()))).rowcount:
                _log(conn, n, actor, "set-add", f"set {name}")
        for n in remove:
            if conn.execute("DELETE FROM secret_set_members WHERE set_id=? AND name=?", (s["id"], n)).rowcount:
                _log(conn, n, actor, "set-remove", f"set {name}")
        s = conn.execute("SELECT * FROM secret_sets WHERE id=?", (s["id"],)).fetchone()
    return {"set": name, "made": made, **_set_info(conn, s)}


def sets(conn):
    return {"sets": [_set_info(conn, s) for s in conn.execute("SELECT * FROM secret_sets ORDER BY name")]}


def _scope_value(conn, scope, value):
    """The stored value of a grant's scope: an id for a project, a goal or a task, else the name; refused when it
    names nothing, or a goal or task that has ended."""
    if scope not in GRANT_SCOPES:
        raise RiverError(f"a grant names one of {', '.join(GRANT_SCOPES)}, not {scope!r}")
    value = str(value or "").strip()
    if not value:
        raise RiverError(f"name the {scope}: maxpm secret grant <set> --{scope} <{scope}>")
    if scope == "project":
        return str(core._project(conn, value)["id"])
    if scope == "goal":
        g = core._goal(conn, value)
        if g["status"] != "open":
            raise RiverError(f"the goal {value} is complete: a goal grant ends with its goal")
        return str(g["id"])
    if scope == "item":
        it = core._item(conn, value.lstrip("#"))
        if it["status"] in core.CLOSED_STATES:
            raise RiverError(f"#{it['id']} is {it['status']}: a task grant ends with its task")
        return str(it["id"])
    if scope == "kind":
        if value.lower() not in GRANT_KINDS:
            raise RiverError(f"a kind grant names one of {', '.join(GRANT_KINDS)}, not {value!r}")
        return value.lower()
    if not conn.execute("SELECT 1 FROM agents WHERE name=?", (value,)).fetchone():
        raise RiverError(f"no agent {value}: maxpm who --all shows the names")
    return value


def _grant_info(conn, g, set_name=None):
    end = _grant_end(conn, g)
    return {"id": g["id"], "grant": f"g{g['id']}", "set": set_name or _set_name(conn, g["set_id"]),
            "scope": g["scope"], "value": g["value"], "covers": _scope_text(conn, g["scope"], g["value"]),
            "note": g["note"], "granted_by": g["granted_by"], "granted_at": g["granted_at"], "until": g["until"],
            "active": not end, "ended": end}


def _set_name(conn, set_id):
    r = conn.execute("SELECT name FROM secret_sets WHERE id=?", (set_id,)).fetchone()
    return r["name"] if r else None


def grant(conn, set_name, scope, value, until=None, note=None, actor=None):
    """Grant a set to one scope, until a revoke or until (a time a person types: 7d, 2026-12-01). A person, or the
    manager for a set marked 'the manager may grant' (#5828)."""
    s = _set(conn, set_name)
    actor = _may_grant(conn, actor, s, "grant")["name"] or actor
    v = _scope_value(conn, scope, value)
    end = core.iso(core.parse_when(until, core.setting(conn, "timezone"))) if until else None
    with core.tx(conn):
        at = core.iso(core.now())
        for g in conn.execute("SELECT * FROM secret_grants WHERE set_id=? AND scope=? AND value=?",
                              (s["id"], scope, v)).fetchall():
            if not _grant_end(conn, g, at):
                if until is not None and g["until"] != end:
                    conn.execute("UPDATE secret_grants SET until=? WHERE id=?", (end, g["id"]))
                g = conn.execute("SELECT * FROM secret_grants WHERE id=?", (g["id"],)).fetchone()
                return {**_grant_info(conn, g, set_name), "already": True}
        cur = conn.execute("INSERT INTO secret_grants (set_id, scope, value, note, granted_by, granted_at, until) "
                           "VALUES (?,?,?,?,?,?,?)", (s["id"], scope, v, note, actor, at, end))
        g = conn.execute("SELECT * FROM secret_grants WHERE id=?", (cur.lastrowid,)).fetchone()
        # #5834: the requests for this set and scope are answered by the grant
        for r in conn.execute("SELECT * FROM secret_requests WHERE state IN ('open','asked') AND scope=? AND value=? "
                              "AND set_name=?", (scope, v, set_name)).fetchall():
            conn.execute("UPDATE secret_requests SET state='granted', decided_by=?, decided_at=?, grant_id=? WHERE id=?",
                         (actor, at, g["id"], r["id"]))
            if r["asked_by"]:
                core._send(conn, "notice", "maxpm", f"{actor or 'a person'} granted the set {set_name} for your "
                           f"request r{r['id']}: run the command again", to=r["asked_by"], item_id=r["item_id"])
    return {**_grant_info(conn, g, set_name), "already": False}


# ---- requests (#5834, mark 2026-11-14): a session asks the manager; for a set the person did not mark, the manager
# proposes a checklist to the person, and the person's OK grants exactly the ticked credentials.

def _people(conn):
    return [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY last_seen DESC")]


def _request(conn, rid):
    t = str(rid).strip().lstrip("rR#")
    r = conn.execute("SELECT * FROM secret_requests WHERE id=?", (int(t),)).fetchone() if t.isdigit() else None
    if not r:
        raise RiverError(f"no request {rid}: maxpm secret requests lists them")
    return r


def request_info(conn, r):
    import json
    d = dict(r)
    d["request"] = f"r{r['id']}"
    for k in ("offered", "ticked", "chosen", "sites"):
        d[k] = json.loads(r[k]) if r[k] else []
    d["covers"] = _scope_text(conn, r["scope"], r["value"])
    it = conn.execute("SELECT title, short FROM items WHERE id=?", (r["item_id"],)).fetchone() if r["item_id"] else None
    d["task"] = (it["short"] or it["title"]) if it else None
    return d


def _checklist(conn, rid, offered, ticked, reason, task, who):
    from . import notify
    lines = "\n".join(f"  [{'x' if n in ticked else ' '}] {n}" for n in offered)
    return (f"{who} asks you to allow credentials for {task} (request r{rid}). Reason: {reason}\n{lines}\n"
            f"Tick or untick them on the page and press OK: {notify.page_url(conn)}settings\n"
            f"Or here: maxpm secret approve r{rid} (the ticked ones), --only <names> (your own pick), "
            f"or maxpm secret refuse r{rid} --note \"<why>\"")


def signin_request(conn, sites, source, reason, actor=None, item=None):
    """A session asks for the person's sign-ins of some sites (#5840, mark 2026-11-14: 'a permissions modal'): the
    cookies of each site in the browser source ('project/session'), where the person signed in. It goes to the person
    at once: the page shows it as a modal over any tab, each site ticked. Allow (routes/secrets.py) saves the ticked
    sites' cookies and grants them to the task the session holds, else to the session; Deny refuses."""
    import json
    who = session_of(conn, actor)
    if who["person"]:
        raise RiverError("a session asks for sign-ins; a person saves one in the Credentials panel")
    if not (reason or "").strip():
        raise RiverError("say why the task needs it: maxpm secret request --signin <site> --from <session> "
                         "--reason \"<why>\"")
    sites = list(dict.fromkeys(norm_site(s) for s in sites if (s or "").strip()))
    if not sites:
        raise RiverError("name the sites: --signin google.com,cloudflare.com")
    project, _, session = (source or "").partition("/")
    if not project:
        raise RiverError("name the browser where the person is signed in: --from <session> (and --from-project)")
    core._project(conn, project)
    people = _people(conn)
    if not people:
        raise RiverError("no person is registered to ask (maxpm register <name> --human)")
    name = who["name"]
    held = core._item(conn, item) if item else core._held_item(conn, name)
    scope, value = ("item", str(held["id"])) if held else ("agent", name)
    task = f"#{held['id']}" if held else name
    from . import notify
    with core.tx(conn):
        cur = conn.execute("INSERT INTO secret_requests (item_id, scope, value, reason, asked_by, asked_at, offered, "
                           "ticked, state, sites, source) VALUES (?,?,?,?,?,?,?,?,'asked',?,?)",
                           (held["id"] if held else None, scope, value, reason.strip(), name, core.iso(core.now()),
                            json.dumps(sites), json.dumps(sites), json.dumps(sites), f"{project}/{session}"))
        rid = cur.lastrowid
        where = f"the browser of {session} ({project})" if session else f"the browser of project {project}"
        core._send(conn, "alert", "maxpm", f"{name} asks for your sign-ins of {', '.join(sites)} from {where} for "
                   f"{task} (request r{rid}). Reason: {reason.strip()}. The page shows it now: Allow or Deny. "
                   f"{notify.page_url(conn)}", to=people[0], item_id=held["id"] if held else None)
    return {**request_info(conn, _request(conn, rid)), "to": people[0]}


def request(conn, set_name, reason, actor=None, item=None):
    """A session asks for a set for its task. To the manager when one is active (it grants a marked set, or proposes
    a checklist to the person for another); with no manager, a set that is not marked goes to the person as a
    checklist of its credentials, all ticked."""
    import json
    who = session_of(conn, actor)
    if who["person"]:
        raise RiverError("a person grants it: maxpm secret grant <set> --item <id>")
    if not (reason or "").strip():
        raise RiverError("say why the task needs it: maxpm secret request <set> --reason \"<why>\"")
    s = _set(conn, set_name)
    name = who["name"]
    held = core._item(conn, item) if item else core._held_item(conn, name)
    scope, value = ("item", str(held["id"])) if held else ("agent", name)
    at = core.iso(core.now())
    manager = core.active_manager(conn, but=name)
    members = [r["name"] for r in conn.execute("SELECT name FROM secret_set_members WHERE set_id=? ORDER BY name",
                                               (s["id"],))]
    people = _people(conn)
    to_person = not manager and not s["manager_may_grant"]
    with core.tx(conn):
        cur = conn.execute("INSERT INTO secret_requests (item_id, scope, value, set_name, reason, asked_by, asked_at, "
                           "offered, ticked, state) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (held["id"] if held else None, scope, value, s["name"], reason.strip(), name, at,
                            json.dumps(members) if to_person else None, json.dumps(members) if to_person else None,
                            "asked" if to_person else "open"))
        rid = cur.lastrowid
        task = f"#{held['id']}" if held else name
        if manager:
            how = (f"maxpm secret grant {s['name']} --{scope} {value}" if s["manager_may_grant"] else
                   f"the set is not marked 'the manager may grant': put a checklist to the person: maxpm secret "
                   f"propose r{rid} --offer {','.join(members) or '<names>'} --tick <the ones you expect>")
            core._send(conn, "alert", "maxpm", f"{name} asks for the set {s['name']} for {task} (request r{rid}): "
                       f"{reason.strip()}. {how}", to=manager, item_id=held["id"] if held else None)
            to = manager
        elif s["manager_may_grant"] or not people:
            to = people[0] if people else None
            if to:
                core._send(conn, "alert", "maxpm", f"{name} asks for the set {s['name']} for {task} (request r{rid}): "
                           f"{reason.strip()}. maxpm secret grant {s['name']} --{scope} {value}", to=to,
                           item_id=held["id"] if held else None)
        else:
            to = people[0]
            core._send(conn, "alert", "maxpm", _checklist(conn, rid, members, members, reason.strip(), task, name),
                       to=to, item_id=held["id"] if held else None)
    return {**request_info(conn, _request(conn, rid)), "to": to}


def propose(conn, rid, offered, ticked=(), reason=None, actor=None):
    """The manager puts a request to the person as a checklist: offered (credential names), ticked (those it
    expects the person to allow). The person ticks or unticks, then OK (approve)."""
    import json
    who = session_of(conn, actor)
    if not core._is_manager(conn, who["name"]):
        raise RiverError("the manager proposes a checklist; a person approves with maxpm secret approve r<id>")
    r = _request(conn, rid)
    if r["state"] not in ("open", "asked"):
        raise RiverError(f"request r{r['id']} is {r['state']}")
    offered = [check_name(n) for n in dict.fromkeys(x.strip() for x in offered if x.strip())]
    if not offered:
        raise RiverError("--offer names the credentials the person chooses from")
    missing = [n for n in offered if not conn.execute("SELECT 1 FROM secrets WHERE name=?", (n,)).fetchone()]
    if missing:
        raise RiverError(f"no credential {', '.join(missing)}: maxpm secret list shows the names")
    ticked = [n for n in dict.fromkeys(x.strip() for x in ticked if x.strip())]
    if any(n not in offered for n in ticked):
        raise RiverError("--tick names only credentials of --offer")
    people = _people(conn)
    if not people:
        raise RiverError("no person is registered to ask (maxpm register <name> --human)")
    reason = (reason or r["reason"] or "").strip()
    with core.tx(conn):
        conn.execute("UPDATE secret_requests SET state='asked', proposed_by=?, proposed_at=?, offered=?, ticked=?, "
                     "reason=? WHERE id=?", (who["name"], core.iso(core.now()), json.dumps(offered), json.dumps(ticked),
                                            reason, r["id"]))
        task = f"#{r['item_id']}" if r["item_id"] else r["asked_by"]
        core._send(conn, "alert", "maxpm", _checklist(conn, r["id"], offered, ticked, reason, task, who["name"]),
                   to=people[0], item_id=r["item_id"])
    return {**request_info(conn, _request(conn, rid)), "to": people[0]}


def approve(conn, rid, chosen=None, actor=None, note=None):
    """The person's OK: a set of exactly the chosen credentials (default: the ticked ones), granted to the request's
    scope. Only a person."""
    import json
    _person_only(conn, actor, "approve a credential request")
    r = _request(conn, rid)
    if r["state"] not in ("open", "asked"):
        raise RiverError(f"request r{r['id']} is {r['state']}")
    pick = json.loads(r["ticked"] or "[]") if chosen is None else [x.strip() for x in chosen if x.strip()]
    if chosen is None and not r["offered"]:  # a request with no checklist: the set it named
        pick = [x["name"] for x in conn.execute("SELECT m.name FROM secret_set_members m JOIN secret_sets s "
                                                "ON s.id=m.set_id WHERE s.name=?", (r["set_name"],))]
    if not pick:
        raise RiverError("nothing is ticked: tick the credentials to allow, or refuse it: maxpm secret refuse "
                         f"r{r['id']} --note \"<why>\"")
    name = f"req{r['id']}"
    set_change(conn, name, add=pick, note=f"request r{r['id']} of {r['asked_by']} for "
               + (f"#{r['item_id']}" if r["item_id"] else r["value"]), actor=actor)
    g = grant(conn, name, r["scope"], r["value"], note=note or f"request r{r['id']}", actor=actor)
    at = core.iso(core.now())
    with core.tx(conn):
        conn.execute("UPDATE secret_requests SET state='granted', decided_by=?, decided_at=?, chosen=?, set_id="
                     "(SELECT id FROM secret_sets WHERE name=?), grant_id=?, note=COALESCE(?, note) WHERE id=?",
                     (actor, at, json.dumps(pick), name, g["id"], note, r["id"]))
        for to in {r["asked_by"], r["proposed_by"]} - {None}:
            core._send(conn, "notice", "maxpm", f"{actor or 'the person'} allowed {', '.join(pick)} for request "
                       f"r{r['id']} ({g['covers']}): run the command again", to=to, item_id=r["item_id"])
    return {**request_info(conn, _request(conn, rid)), "grant": g["grant"]}


def refuse(conn, rid, note=None, actor=None):
    """No grant: a person, or the manager for a request that is still at it."""
    who = session_of(conn, actor)
    r = _request(conn, rid)
    if not (who["person"] or (core._is_manager(conn, who["name"]) and r["state"] == "open")):
        raise RiverError("only a person refuses a request that is at the person, and the manager one at the manager")
    if r["state"] not in ("open", "asked"):
        raise RiverError(f"request r{r['id']} is {r['state']}")
    with core.tx(conn):
        conn.execute("UPDATE secret_requests SET state='refused', decided_by=?, decided_at=?, note=? WHERE id=?",
                     (who["name"] or actor, core.iso(core.now()), note, r["id"]))
        if r["asked_by"]:
            core._send(conn, "notice", "maxpm", f"request r{r['id']} for the set {r['set_name']} was refused"
                       + (f": {note}" if note else "") + ". Do the task without it, or release it",
                       to=r["asked_by"], item_id=r["item_id"])
    return request_info(conn, _request(conn, rid))


def requests(conn, every=False):
    q = "SELECT * FROM secret_requests" + ("" if every else " WHERE state IN ('open','asked')") + " ORDER BY id"
    return {"requests": [request_info(conn, r) for r in conn.execute(q)]}


def check(conn, agent, limit=30):
    """For one session (#5834, mark: 'a way to verify that they are getting credentials they are actually allowed to
    access'): the grants that cover it now and who gave each, its requests, and its uses and refusals."""
    if not conn.execute("SELECT 1 FROM agents WHERE name=?", (agent,)).fetchone():
        raise RiverError(f"no agent {agent}: maxpm who --all shows the names")
    return {"agent": agent, "grants": grants(conn, for_agent=agent)["grants"],
            "requests": [request_info(conn, r) for r in conn.execute(
                "SELECT * FROM secret_requests WHERE asked_by=? ORDER BY id DESC LIMIT 10", (agent,))],
            "uses": log(conn, limit=limit, actor=agent)["log"]}


def revoke(conn, grant_id=None, set_name=None, scope=None, value=None, actor=None):
    """End grants: one by its id (g12), or the set's grants to one scope, or all grants of the set. The next use is
    refused. A person, or the manager for a set marked 'the manager may grant' (#5828)."""
    if grant_id is not None:
        gid = str(grant_id).lstrip("gG#")
        if not gid.isdigit():
            raise RiverError(f"a grant id is g<number> (maxpm secret grants), not {grant_id!r}")
        rows = conn.execute("SELECT * FROM secret_grants WHERE id=?", (int(gid),)).fetchall()
        if not rows:
            raise RiverError(f"no grant g{gid}: maxpm secret grants shows them")
    else:
        s = _set(conn, set_name)
        q, args = "SELECT * FROM secret_grants WHERE set_id=?", [s["id"]]
        if scope:
            q += " AND scope=? AND value=?"
            args += [scope, _scope_value(conn, scope, value)]
        rows = conn.execute(q, args).fetchall()
    for set_id in {g["set_id"] for g in rows}:
        actor = _may_grant(conn, actor, conn.execute("SELECT * FROM secret_sets WHERE id=?", (set_id,)).fetchone(),
                           "revoke")["name"] or actor
    if not rows:  # a set with no grants (a grant id with none raised above): the same rule says who learns that
        _may_grant(conn, actor, s, "revoke")
    at = core.iso(core.now())
    live = [g for g in rows if not _grant_end(conn, g, at)]
    with core.tx(conn):
        for g in live:
            conn.execute("UPDATE secret_grants SET revoked_at=?, revoked_by=? WHERE id=?", (at, actor, g["id"]))
    return {"revoked": [_grant_info(conn, conn.execute("SELECT * FROM secret_grants WHERE id=?", (g["id"],)).fetchone())
                        for g in live]}


SUMMARY_EVERY = 86400  # seconds: the person's summary of the manager's grants comes once a day


def grant_summary(conn):
    """A pass of maxpm serve (#5828): once a day, a note to the person with the grants and revokes that a session
    (the manager) made since the last summary; nothing when it made none."""
    import datetime
    r = conn.execute("SELECT value FROM meta WHERE key='secret_grant_summary_at'").fetchone()
    now = core.now()
    if r and core.parse_iso(r["value"]) > now - datetime.timedelta(seconds=SUMMARY_EVERY):
        return None
    since = r["value"] if r else core.iso(now - datetime.timedelta(seconds=SUMMARY_EVERY))
    people = {x["name"] for x in conn.execute("SELECT name FROM agents WHERE kind='human'")}
    lines = []
    for g in conn.execute("SELECT * FROM secret_grants WHERE granted_at>? OR revoked_at>? ORDER BY id", (since, since)):
        if g["granted_at"] > since and g["granted_by"] and g["granted_by"] not in people:
            lines.append(f"granted by {g['granted_by']}: " + _grant_line_text(conn, g))
        if g["revoked_at"] and g["revoked_at"] > since and g["revoked_by"] and g["revoked_by"] not in people:
            lines.append(f"revoked by {g['revoked_by']}: " + _grant_line_text(conn, g))
    person = conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY last_seen DESC LIMIT 1").fetchone()
    with core.tx(conn):
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('secret_grant_summary_at', ?)", (core.iso(now),))
        if lines and person:
            core._send(conn, "note", "maxpm", "The manager's credential grants of the last day:\n  " + "\n  ".join(lines)
                       + "\nEnd one: maxpm secret revoke g<id>. Mark a set so the manager may no longer grant it: "
                         "maxpm secret set <set> --manager-may-grant off", to=person["name"])
    return lines


def _grant_line_text(conn, g):
    info = _grant_info(conn, g)
    return f"g{g['id']} set {info['set']} to {info['covers']}" + (f" until {core.show_time(g['until'])}" if g["until"]
                                                                   else "")


def grants(conn, set_name=None, ended=False, for_agent=None):
    """The grants that give their set now (with ended: also the ended ones), of one set, or those that cover one
    agent now."""
    q, args = "SELECT * FROM secret_grants", []
    if set_name:
        q += " WHERE set_id=?"
        args.append(_set(conn, set_name)["id"])
    rows = [_grant_info(conn, g) for g in conn.execute(q + " ORDER BY id", args)]
    if for_agent:
        scopes = _scopes_of(conn, for_agent)
        rows = [g for g in rows if _covers(g, scopes)]
    if not ended:
        rows = [g for g in rows if g["active"]]
    return {"grants": rows}
