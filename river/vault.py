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
    with core.tx(conn):
        conn.execute("DELETE FROM secrets WHERE name=?", (name,))
        _log(conn, name, actor, "delete")
    return {"secret": name, "deleted": True}


def listing(conn, project=None):
    q = ("SELECT s.name, p.name AS project, s.note, s.created_by, s.created_at, s.updated_by, s.updated_at, s.used_at, s.used_by, "
         "s.uses, s.level, s.allow_commands, s.allow_hosts, s.allow_env, s.var "
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


# ---- sets and grants (#5822)

GRANT_SCOPES = ("project", "goal", "kind", "item", "agent")
# The kinds of task that a kind grant names: the task kinds of maxpm add, and the kinds that MaximizePM makes.
GRANT_KINDS = ("work", "operational", "review", "fixes", "deploy", "monitor")


def session_of(conn, actor):
    """Who asks, for every grant check and grant change: {'name', 'person'}. Until sessions prove who they are
    (#5823), it is the name that the command gives (--as, MAXPM_AGENT); no name, or a person's name, is a person.
    #5823 replaces this function only."""
    return {"name": actor, "person": not actor or core._is_person(conn, actor)}


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


def allowed(conn, name, actor):
    """Whether actor may use (or replace, or delete) the secret name: {'ok', 'by', 'sets', 'why'}. 'by' says what
    allowed it (a person, the saver, grant g<id>); 'why' says what is missing."""
    who = session_of(conn, actor)
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
    with core.tx(conn):
        _log(conn, name, actor, "refused", f"{detail or what}: {ok['why']}")
    held = core._held_item(conn, actor)
    scope = f"--item {held['id']}" if held else f"--agent {actor}"
    if ok["sets"]:
        ask = f"Grant the set {ok['sets'][0]} to me: maxpm secret grant {ok['sets'][0]} {scope}"
    else:
        ask = f"Put the secret {name} in a set and grant it to me: maxpm secret set <set> --add {name}; " \
              f"maxpm secret grant <set> {scope}"
    raise RiverError(f"{ok['why']}: the {what} is refused. Ask the manager: "
                     + _ask_line(conn, ask, held["id"] if held else None))


def _person_only(conn, actor, what):
    if not session_of(conn, actor)["person"]:
        raise RiverError(f"only a person {what}. Ask the manager to bring it to the person: "
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
    _person_only(conn, actor, "changes a credential set")
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
    """Grant a set to one scope, until a revoke or until (a time a person types: 7d, 2026-12-01). Only a person."""
    _person_only(conn, actor, "grants a credential set")
    s = _set(conn, set_name)
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
    return {**_grant_info(conn, g, set_name), "already": False}


def revoke(conn, grant_id=None, set_name=None, scope=None, value=None, actor=None):
    """End grants: one by its id (g12), or the set's grants to one scope, or all grants of the set. The next use is
    refused. Only a person."""
    _person_only(conn, actor, "revokes a credential set's grant")
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
    at = core.iso(core.now())
    live = [g for g in rows if not _grant_end(conn, g, at)]
    with core.tx(conn):
        for g in live:
            conn.execute("UPDATE secret_grants SET revoked_at=?, revoked_by=? WHERE id=?", (at, actor, g["id"]))
    return {"revoked": [_grant_info(conn, conn.execute("SELECT * FROM secret_grants WHERE id=?", (g["id"],)).fetchone())
                        for g in live]}


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
