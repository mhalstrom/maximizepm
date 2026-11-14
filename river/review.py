"""The Changes view (#5855, mark 2026-11-14, layer 2 of the plan: a person's review of what the agents change).

What a session changed in its folder, against a base the person picks: 'origin' (the merge base of HEAD and the
GitHub main branch: everything committed or not that GitHub does not have yet), 'uncommitted' (against HEAD), or
'review' (against the commit of the person's last Reviewed mark). What an item changed: the commits whose message
names #<id>, also after the push; a release (a deploy item): the commits of every item it ships.

It works for any agent: git in the folder, and the session's messages (comments go to the session as one alert).
The project setting review_at (off, push, release) decides whether a push or a release waits for Approve.

Every git command reads only; nothing here edits a file."""

import os
import re
import subprocess
import threading
import time

from . import core
from .core import RiverError

MAX_LINES = 4000       # the diff lines one answer carries; the rest is cut (the file list stays whole)
NEW_FILE_MAX = 400     # lines of an untracked file shown as a new file
UNTRACKED_MAX = 40     # untracked files shown; the rest are named in the list only
GIT_WAIT = 20          # seconds a git command may take
BASES = ("origin", "uncommitted", "review")
RUNNER = None          # tests: fn(folder, args) -> stdout, in place of git


def git(folder, *args, ok=(0,)):
    """stdout of git in folder, or None when git refuses (not a repository, an unknown ref)."""
    if RUNNER:
        return RUNNER(folder, args)
    try:
        r = subprocess.run(["git", "-C", folder, *args], capture_output=True, timeout=GIT_WAIT,
                           env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.decode(errors="replace") if r.returncode in ok else None


# ---- where a session works

def folder_of(conn, agent):
    """The folder a session works in: its chat log's last cwd, else its process's working folder, else the folder of
    the project of the item it holds. None when none is known."""
    from . import sessions
    try:
        kind, path = sessions.transcript(conn, agent)
        if path:
            f = sessions.last_cwd(path)
            if f and os.path.isdir(f):
                return f
    except (OSError, ValueError, EOFError):
        pass
    a = conn.execute("SELECT pid FROM agents WHERE name=?", (agent,)).fetchone()
    if a and a["pid"]:
        f = core.proc_cwd(a["pid"])
        if f and os.path.isdir(f):
            return f
    held = core._held_item(conn, agent)
    if held:
        p = conn.execute("SELECT path FROM projects WHERE id=?", (held["project_id"],)).fetchone()
        if p and p["path"] and os.path.isdir(p["path"]):
            return p["path"]
    return None


def repo_root(folder):
    out = git(folder, "rev-parse", "--show-toplevel")
    return out.strip() if out else None


def main_ref(root):
    """The GitHub main branch of the repository: origin/HEAD, else origin/main or origin/master, else main or master."""
    out = git(root, "rev-parse", "--abbrev-ref", "origin/HEAD")
    if out and out.strip() and out.strip() != "origin/HEAD":
        return out.strip()
    for ref in ("origin/main", "origin/master", "main", "master"):
        if git(root, "rev-parse", "--verify", "--quiet", ref) is not None:
            return ref
    return None


# ---- the diff, parsed

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse(text, budget):
    """A unified diff as files: [{path, old, status, add, del, binary, hunks: [{head, lines: [[t, old, new, text]]}]}].
    t is '+', '-' or ' '. budget: {'left': lines}; a file past it keeps its counts and no lines ('cut')."""
    files, f, h, o, n = [], None, None, 0, 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            f = {"path": m.group(2) if m else line[11:], "old": m.group(1) if m else None, "status": "modified",
                 "add": 0, "del": 0, "binary": False, "hunks": [], "cut": False}
            files.append(f)
            h = None
        elif f is None:
            continue
        elif line.startswith("new file mode"):
            f["status"] = "added"
        elif line.startswith("deleted file mode"):
            f["status"] = "deleted"
        elif line.startswith("rename from "):
            f["status"] = "renamed"
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            f["binary"] = True
        elif line.startswith("@@"):
            m = HUNK.match(line)
            if not m:
                continue
            o, n = int(m.group(1)), int(m.group(3))
            h = {"head": line, "lines": []}
            if budget["left"] > 0:
                f["hunks"].append(h)
            else:
                f["cut"] = True
        elif h is not None and line[:1] in ("+", "-", " ") and not line.startswith(("+++ ", "--- ")):
            t = line[0]
            if t == "+":
                f["add"] += 1
                row = ["+", None, n, line[1:]]
                n += 1
            elif t == "-":
                f["del"] += 1
                row = ["-", o, None, line[1:]]
                o += 1
            else:
                row = [" ", o, n, line[1:]]
                o += 1
                n += 1
            if budget["left"] > 0 and h in f["hunks"]:
                h["lines"].append(row)
                budget["left"] -= 1
            elif h in f["hunks"]:
                f["cut"] = True
    return files


def untracked(root, budget):
    """The files not yet known to git (not ignored), each as a new file with its text (text files only)."""
    out = git(root, "ls-files", "--others", "--exclude-standard", "-z")
    names = [x for x in (out or "").split("\0") if x]
    files = []
    for i, name in enumerate(names):
        f = {"path": name, "old": None, "status": "added", "add": 0, "del": 0, "binary": False, "hunks": [],
             "cut": False, "untracked": True}
        files.append(f)
        if i >= UNTRACKED_MAX or budget["left"] <= 0:
            f["cut"] = True
            continue
        try:
            with open(os.path.join(root, name), "rb") as fh:
                data = fh.read(256 * 1024)
        except OSError:
            continue
        if b"\0" in data[:8192]:
            f["binary"] = True
            continue
        lines = data.decode(errors="replace").splitlines()
        f["add"] = len(lines)
        keep = lines[:min(NEW_FILE_MAX, budget["left"])]
        f["hunks"] = [{"head": f"@@ -0,0 +1,{len(lines)} @@", "lines": [["+", None, i + 1, x] for i, x in enumerate(keep)]}]
        f["cut"] = len(keep) < len(lines)
        budget["left"] -= len(keep)
    return files


# ---- a session's changes

def marks(conn, target):
    """The person's marks of a target, newest first: [{kind, sha, by, at}]."""
    return [{"kind": r["kind"], "sha": r["sha"], "by": r["approved_by"], "at": r["approved_at"]} for r in conn.execute(
        "SELECT * FROM diff_approvals WHERE target=? ORDER BY id DESC LIMIT 50", (target,))]


def state_of(conn, target, sha):
    """'approved', 'changes' (Request changes) or None: the newest Approve or Request changes mark of this commit."""
    r = conn.execute("SELECT kind FROM diff_approvals WHERE target=? AND sha=? AND kind IN ('approved','changes') "
                     "ORDER BY id DESC LIMIT 1", (target, sha)).fetchone()
    return r["kind"] if r else None


def sharing(conn, agent, root):
    """The other running sessions whose folder is the same repository: their changes are in this diff too."""
    return [a for a in live_agents(conn) if a != agent and (lambda f: f and (repo_root(f) or f) == root)(
        folder_of(conn, a))]


def live_agents(conn):
    """The names of the agent sessions that are not gone or stopped."""
    return [r["name"] for r in conn.execute("SELECT * FROM agents WHERE kind='ai' ORDER BY name").fetchall()
            if core._agent_state(conn, r) not in ("gone", "stopped")]


def session_diff(conn, agent, base="origin"):
    """What the session changed, against base (BASES). Raises RiverError when its folder or repository is unknown."""
    if base not in BASES:
        raise RiverError(f"the base is one of {', '.join(BASES)}, not {base!r}")
    if not conn.execute("SELECT 1 FROM agents WHERE name=?", (agent,)).fetchone():
        raise RiverError(f"no session {agent}")
    folder = folder_of(conn, agent)
    if not folder:
        raise RiverError(f"the folder of {agent} is not known yet: it shows once the session has run a command")
    root = repo_root(folder)
    if not root:
        return {"agent": agent, "folder": folder, "git": False, "files": [], "base": base}
    head = (git(root, "rev-parse", "HEAD") or "").strip() or None
    target = f"agent:{agent}"
    reviewed = next((m for m in marks(conn, target) if m["kind"] == "reviewed"), None)
    ref, against = None, ""
    if base == "review" and reviewed and git(root, "cat-file", "-e", reviewed["sha"] + "^{commit}") is not None:
        ref, against = reviewed["sha"], f"your last review ({reviewed['sha'][:8]})"
    elif base in ("origin", "review"):
        main = main_ref(root)
        mb = (git(root, "merge-base", "HEAD", main) or "").strip() if main and head else ""
        ref = mb or None
        against = (f"{main} (merge base {mb[:8]})" if mb else "no main branch: everything") \
            + (" (no review yet)" if base == "review" else "")
    else:
        ref, against = ("HEAD" if head else None), "the last commit (HEAD)"
    budget = {"left": MAX_LINES}
    text = git(root, "diff", "-M", "--no-color", "--no-ext-diff", ref) if ref else \
        git(root, "diff", "-M", "--no-color", "--no-ext-diff", "--cached")
    files = parse(text or "", budget) + untracked(root, budget)
    project = core.projects_for_dir(conn, root)
    review_at = core.setting(conn, "review_at", project_id=core._project(conn, project[0])["id"]) if project else "off"
    ahead = git(root, "rev-list", "--count", f"{main_ref(root)}..HEAD") if head and main_ref(root) else None
    return {"agent": agent, "folder": folder, "root": root, "git": True, "base": base, "against": against,
            "head": head, "files": files, "cut": budget["left"] <= 0, "review_at": review_at,
            "project": project[0] if project else None, "state": state_of(conn, target, head) if head else None,
            "reviewed": reviewed, "shared": sharing(conn, agent, root),
            "commits_ahead": int(ahead.strip()) if ahead and ahead.strip().isdigit() else None}


# ---- an item's changes (also after the push), a release's

def item_commits(root, item_ids):
    """The commits of the repository whose message names one of the items, oldest first: [{sha, subject, items}]."""
    if not item_ids:
        return []
    out = []
    text = git(root, "log", "--all", "--topo-order", "--format=%H%x00%s%x00%b%x1e",
               *[f"--grep=#{i}" for i in item_ids]) or ""
    for rec in text.split("\x1e"):
        parts = rec.strip("\n").split("\x00")
        if len(parts) < 3 or not parts[0]:
            continue
        sha, subject, body = parts[0].strip(), parts[1], parts[2]
        named = [i for i in item_ids if re.search(rf"#{i}\b", subject + "\n" + body)]
        if named:
            out.append({"sha": sha, "subject": subject, "item": named[0]})
    return out[::-1]  # git log is newest first


def item_diff(conn, item_id):
    """What an item changed: one block per commit that names it; a deploy item: the commits of the items it ships."""
    it = core._item(conn, item_id)
    p = conn.execute("SELECT name, path FROM projects WHERE id=?", (it["project_id"],)).fetchone()
    ids = [it["id"]]
    if it["kind"] == "deploy":
        ids = [r["id"] for r in conn.execute("SELECT i.id, i.project_id FROM deps d JOIN items i ON i.id=d.blocked_by "
                                             "WHERE d.item_id=? AND i.kind NOT IN ('review','deploy') ORDER BY i.id",
                                             (it["id"],))]
    folders = {}
    for i in ids:
        q = conn.execute("SELECT p.path FROM items x JOIN projects p ON p.id=x.project_id WHERE x.id=?", (i,)).fetchone()
        path = (q["path"] if q else None) or (p["path"] if p else None)
        if path and os.path.isdir(path):
            root = repo_root(path)
            if root:
                folders.setdefault(root, []).append(i)
    budget, blocks = {"left": MAX_LINES}, []
    for root, its in folders.items():
        for c in item_commits(root, its):
            text = git(root, "show", "-M", "--no-color", "--no-ext-diff", "--format=", c["sha"]) or ""
            blocks.append({**c, "root": root, "files": parse(text, budget)})
    return {"item": it["id"], "kind": it["kind"], "title": it["title"], "items": ids, "commits": blocks,
            "cut": budget["left"] <= 0, "folder": next(iter(folders), None),
            "review_at": release_review(conn, it["id"]) if it["kind"] == "deploy" else
            core.setting(conn, "review_at", project_id=it["project_id"]),
            "waits": it["blocked_set_by"] == REVIEW_BY, "state": state_of(conn, f"item:{it['id']}", "-")}


def release_review(conn, dep_id):
    """review_at of a release: 'release' when the project of the release or of any item it ships says so (a deploy
    item lives in the target's own project), else the strongest other value."""
    rows = conn.execute("SELECT DISTINCT i.project_id FROM deps d JOIN items i ON i.id=d.blocked_by WHERE d.item_id=? "
                        "UNION SELECT project_id FROM items WHERE id=?", (dep_id, dep_id)).fetchall()
    vals = {core.setting(conn, "review_at", project_id=r[0]) for r in rows}
    return next((v for v in ("release", "push") if v in vals), "off")


# ---- the person's answer

REVIEW_BY = "maxpm review"  # the blocked_set_by of a release that waits for the person's Approve (review_at release)


def send(conn, comments, agent=None, item=None, actor=None, request=False):
    """The person's comments as one alert to the session (or to the holder of the item): each '<file>:<line>' with
    the line it is on, then the text. request: Request changes (the push stays held)."""
    if not comments:
        raise RiverError("no comments to send: click a line, write a comment, then Send to agent")
    lines = []
    for c in comments:
        where = f"{c.get('file')}:{c.get('line')}" + (f"-{c['end']}" if c.get("end") and c.get("end") != c.get("line") else "")
        quote = " ".join(str(c.get("quote") or "").split())[:160]
        lines.append(f"- {where}" + (f" (`{quote}`)" if quote else "") + f": {str(c.get('text') or '').strip()}")
    head = ("The person requests changes" if request else "The person's review comments") + \
        (f" on #{item}" if item else "") + ":\n"
    body = head + "\n".join(lines) + ("\nChange these, commit, and ask again: the push waits for Approve." if request
                                      else "")
    if agent:
        return core.send(conn, "alert", body, to=agent, actor=actor)
    return core.send(conn, "alert", body, item=item, actor=actor)


def mark(conn, kind, agent=None, item=None, sha=None, actor=None):
    """Reviewed, Approve or Request changes. A session: the mark is of its HEAD now. A release that waits for the
    person's Approve (review_at release): Approve ends the wait. Returns the mark."""
    if kind not in ("reviewed", "approved", "changes"):
        raise RiverError("a mark is reviewed, approved or changes")
    if agent:
        folder = folder_of(conn, agent)
        root = repo_root(folder) if folder else None
        sha = sha or ((git(root, "rev-parse", "HEAD") or "").strip() if root else None)
        if not sha:
            raise RiverError(f"no commit to mark in the folder of {agent}")
        target = f"agent:{agent}"
    elif item:
        it = core._item(conn, item)
        target, sha = f"item:{it['id']}", sha or "-"
    else:
        raise RiverError("name the session or the item")
    with core.tx(conn):
        conn.execute("INSERT INTO diff_approvals (target, sha, kind, approved_by, approved_at) VALUES (?,?,?,?,?)",
                     (target, sha, kind, actor, core.iso(core.now())))
        if item:
            it = core._item(conn, item)
            core._event(conn, it["id"], actor, {"approved": "the person approved its changes",
                                                "changes": "the person requested changes",
                                                "reviewed": "the person reviewed its changes"}[kind])
            if kind == "approved" and it["blocked_set_by"] == REVIEW_BY:
                conn.execute("UPDATE items SET blocked_reason=NULL, blocked_until=NULL, blocked_at=NULL, "
                             "blocked_set_by=NULL WHERE id=?", (it["id"],))
                core._event(conn, it["id"], actor, "the release's changes are approved: it may go out")
        elif kind == "approved":
            core._send(conn, "notice", "maxpm", f"{actor or 'The person'} approved your changes at {sha[:8]}: you may "
                       "push them.", to=agent)
    return {"target": target, "sha": sha, "kind": kind}


def push_held(conn, agent, command):
    """Why a session's push waits for the person's review (review_at push), or None. Inside push_guard."""
    folder = folder_of(conn, agent)
    root = repo_root(folder) if folder else None
    if not root:
        return None
    project = core.projects_for_dir(conn, root)
    if not project or core.setting(conn, "review_at", project_id=core._project(conn, project[0])["id"]) != "push":
        return None
    head = (git(root, "rev-parse", "HEAD") or "").strip()
    if not head or state_of(conn, f"agent:{agent}", head) == "approved":
        return None
    from . import notify
    link = notify.page_url(conn, terminal=agent, changes=True)
    since = core.iso(core.now() - core.timedelta(hours=1))
    if not conn.execute("SELECT 1 FROM messages WHERE from_agent='maxpm' AND body LIKE ? AND created_at>?",
                        (f"%review of {agent}'s changes at {head[:8]}%", since)).fetchone():
        people = [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY last_seen DESC")]
        if people:
            with core.tx(conn):
                core._send(conn, "alert", "maxpm", f"{agent} waits for your review of {agent}'s changes at {head[:8]} "
                           f"before it pushes ({project[0]}: review before a push). Changes tab: {link}",
                           to=people[0])
    wanted = state_of(conn, f"agent:{agent}", head)
    return (f"MaximizePM holds this push: the person reviews the changes first (review_at push in {project[0]})"
            + (": they requested changes; read your inbox" if wanted == "changes" else "")
            + f". Wait for the notice that they approved {head[:8]}, then push again.")


# ---- the count of changed files of each session, for its row (kept fresh in a thread, never in the state build)

_COUNTS = {"at": 0.0, "n": {}, "busy": False}
COUNT_AGAIN = 30.0


def counts(conn_factory=None):
    """{agent: changed files against origin} as last counted; starts a count in a thread when it is older than
    COUNT_AGAIN. The page's state build never waits for git."""
    if time.time() - _COUNTS["at"] > COUNT_AGAIN and not _COUNTS["busy"]:
        _COUNTS["busy"] = True
        threading.Thread(target=_count, args=(conn_factory or core.connect,), daemon=True,
                         name="maxpm-review-counts").start()
    return dict(_COUNTS["n"])


def count_of(conn, agent):
    """The number of files the session changed against GitHub main (committed or not, new files too), or None."""
    f = folder_of(conn, agent)
    root = repo_root(f) if f else None
    if not root:
        return None
    main = main_ref(root)
    mb = (git(root, "merge-base", "HEAD", main) or "").strip() if main else ""
    names = set(x for x in ((git(root, "diff", "--name-only", mb) or "") if mb else "").split("\n") if x)
    names |= set(x for x in (git(root, "ls-files", "--others", "--exclude-standard") or "").split("\n") if x)
    return len(names)


def _count(factory):
    try:
        conn = factory()
        try:
            out = {}
            for name in live_agents(conn):
                out_n = count_of(conn, name)
                if out_n is not None:
                    out[name] = out_n
            _COUNTS["n"] = out
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - a count that failed shows the last one
        import sys
        print(f"maxpm: changes count: {e.__class__.__name__}: {e}", file=sys.stderr)
    finally:
        _COUNTS["at"] = time.time()
        _COUNTS["busy"] = False
