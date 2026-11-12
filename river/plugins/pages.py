"""The page walk check (#5516, canvas problem-kinds v3, build task 14): browser errors with their source lines.
Deterministic, no model call.

maxpm ops pages run <name> --project <project> --pages <address>,<address> [--settle 3] [--timeout 60]
asks maxpm serve to open each address in a background tab of the project's browser (the one the Sessions panel
shows, with the project's sign-ins; the tab the person sees does not change), and records each uncaught exception,
console.error call and error log entry with its stack frames, and each request that failed (HTTP 400 or more, or no
answer). The frames are minified as the browser ran them; this plugin maps them back to the source:

- The source maps come from the commit in production: the cut commit of the last release of the project's deploy
  target (HEAD of the project's folder when no release names one). The project's system description says how to
  build them, in code.source_maps, as a line 'command: <shell command>'. MaximizePM copies that commit out of git
  (git archive, so the project's own checkout does not change), runs the command there, and keeps every .map file
  that lies beside its script (x.js.map beside x.js) in <queue folder>/ops/maps/<project>/<commit>/, once for each
  commit. The command's time limit is the setting ops_maps_timeout. maxpm ops pages maps <project> builds them ahead.
- A frame is mapped only when the script that the browser ran has the same SHA-256 as the built script of that name
  (the bundle hash check); else the frame stays minified and says why.

Errors group by their kind, their text with numbers and ids made general, and their top frame; a request groups by
its status, method and address shape. Each group is one subject ('<kind>-<hash>'), so a plugin check with
--subject on has one fault for each group, and a group that no longer comes heals. The noise rules of the check
(MAXPM_NOISE) drop the groups whose text they match. It prints on the Monitoring Plugins API:

  PAGES OK|CRITICAL|UNKNOWN - <summary> | groups=<n> pages=<n>
  <subject>: CRITICAL <the error> (<count>x on <pages>)
    at <function> (<source file>:<line>:<column>)      (its frames, the source ones when mapped)
  {"groups": [...], "maps": {...}, "pages": n, "skipped": n}

and exits 0 (no error on any page), 2 (an error group) or 3 (the walk did not run, or no page was walked)."""

import bisect
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from .. import core
from ..core import RiverError

WALK = None       # a test hook: (conn, project, pages, settle, timeout) -> the result of browser.walk
FRAMES_SHOWN = 5  # the frames of a group in the output lines
GROUPS_KEPT = 30  # the groups in the evidence object
FAIL_AGAIN = 3600  # seconds before a failed build of one commit runs again
STATUS = {0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"}
_B64 = {c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")}


# ---------------------------------------------------------------- source maps (version 3)

def _vlq(seg):
    """The numbers of one segment of a source map's mappings (Base64 VLQ)."""
    out, val, shift = [], 0, 0
    for ch in seg:
        d = _B64[ch]
        val += (d & 31) << shift
        if d & 32:
            shift += 5
            continue
        out.append(-(val >> 1) if val & 1 else val >> 1)
        val = shift = 0
    return out


def clean_source(s):
    """A source path of a map as a path in the repository: no scheme (webpack://app/), no leading ./ or ../."""
    s = re.sub(r"^[a-z][\w+.-]*://[^/]*/", "", s or "")
    s = s.lstrip("/")
    while s.startswith(("./", "../")):
        s = s[s.index("/") + 1:]
    return s


class SourceMap:
    """A source map: lookup(line, column) of a generated position (0-based) gives its source, line and column
    (1-based) and name. An index map (sections) is read as its sections."""

    def __init__(self, data):
        self.sections = None
        if "sections" in data:
            self.sections = [((x["offset"]["line"], x["offset"]["column"]), SourceMap(x["map"]))
                             for x in data["sections"] if "map" in x]
            return
        root = data.get("sourceRoot") or ""
        self.sources = [clean_source(posixpath.join(root, s) if root else s) for s in data.get("sources") or []]
        self.names = data.get("names") or []
        self.rows, self.cols = [], []
        src = line = col = name = 0
        for text in (data.get("mappings") or "").split(";"):
            gen, row = 0, []
            for seg in text.split(","):
                if not seg:
                    continue
                v = _vlq(seg)
                gen += v[0]
                if len(v) >= 4:
                    src, line, col = src + v[1], line + v[2], col + v[3]
                    n = None
                    if len(v) >= 5:
                        name += v[4]
                        n = name
                    row.append((gen, src, line, col, n))
            row.sort(key=lambda r: r[0])
            self.rows.append(row)
            self.cols.append([r[0] for r in row])

    def lookup(self, line, column):
        if self.sections is not None:
            best = None
            for (ol, oc), m in self.sections:
                if (ol, oc) <= (line, column):
                    best = (ol, oc, m)
            if best is None:
                return None
            ol, oc, m = best
            return m.lookup(line - ol, column - oc if line == ol else column)
        if not 0 <= line < len(self.rows):
            return None
        i = bisect.bisect_right(self.cols[line], column) - 1
        if i < 0:
            return None
        _, s, sl, sc, n = self.rows[line][i]
        return {"source": self.sources[s] if 0 <= s < len(self.sources) else "?", "line": sl + 1, "column": sc + 1,
                "name": self.names[n] if n is not None and 0 <= n < len(self.names) else None}


# ---------------------------------------------------------------- the maps of a commit

def map_dir(project, rev):
    return core.db_path().parent / "ops" / "maps" / project / rev


def map_command(conn, project):
    """The source map command of the project's system description: its line 'command: <shell command>' in
    code.source_maps, or None."""
    shown = core.ops_system_show(conn, project)["shown"]  # the version in force
    text = (((shown or {}).get("body") or {}).get("code") or {}).get("source_maps") or ""
    for line in text.splitlines():
        m = re.match(r"^\s*command:\s*(.+?)\s*$", line)
        if m:
            return m.group(1)
    return None


def released(conn, project, rev=None):
    """(folder, commit, note) of the build to map with: rev when given; else the cut commit of the last release of
    the project's target; else HEAD of the project's folder. folder or commit None: note says why."""
    r = conn.execute("SELECT path, target FROM projects WHERE name=?", (project,)).fetchone()
    folder = os.path.expanduser(r["path"]) if r and r["path"] else None
    if not folder or not core._git_commit(folder):
        return None, None, f"the project {project} has no git folder (maxpm project path {project} <folder>)"
    if rev:
        c = core._git_commit(folder, rev)
        return folder, c, None if c else f"no commit {rev!r} in {folder}"
    if r["target"]:
        d = conn.execute("SELECT cut_rev FROM items WHERE kind='deploy' AND target=? AND status='done' AND cut_rev IS "
                         "NOT NULL ORDER BY closed_at DESC, id DESC LIMIT 1", (r["target"],)).fetchone()
        c = core._git_commit(folder, d["cut_rev"]) if d else None
        if c:
            return folder, c, None
    return folder, core._git_commit(folder), (f"no release of {r['target']} names a commit" if r["target"] else
                                              "the project has no deploy target") + ": the build of HEAD, and the " \
                                                                                   "hash check decides"


def _extract(data, into):
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        if hasattr(tarfile, "data_filter"):
            t.extractall(into, filter="data")
        else:  # Python 3.10 and 3.11 before the filter: git archive writes no absolute or parent paths
            t.extractall(into)


def build_maps(conn, project, rev=None):
    """The source maps of the commit in production (or rev): {"rev", "files": {script name: [{map, sha256, file}]},
    "note"}, built once for each commit and kept; or {"rev", "error"} when they cannot be built. A failed build of a
    commit runs again after FAIL_AGAIN seconds."""
    folder, commit, note = released(conn, project, rev)
    if not commit:
        return {"rev": None, "error": note}
    cmd = map_command(conn, project)
    if not cmd:
        return {"rev": commit, "error": f"the system description of {project} names no source map command: maxpm ops "
                                        f"system edit {project} --set \"code.source_maps=command: <the build command "
                                        f"that writes .map files>\""}
    d = map_dir(project, commit)
    idx, failed = d / "index.json", d / "failed.json"
    if idx.is_file():
        return dict(json.loads(idx.read_text(encoding="utf-8")), note=note)
    if failed.is_file() and time.time() - failed.stat().st_mtime < FAIL_AGAIN:
        return {"rev": commit, "error": json.loads(failed.read_text(encoding="utf-8"))["error"]}
    d.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="maxpm-maps-"))
    try:
        try:
            a = subprocess.run(["git", "-C", folder, "archive", "--format=tar", commit], capture_output=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as e:
            a = subprocess.CompletedProcess([], 1, b"", str(e).encode())
        if a.returncode != 0:
            err = f"git archive {commit[:12]} failed: {a.stderr.decode(errors='replace').strip()[:300]}"
        else:
            _extract(a.stdout, work)
            limit = core.parse_duration(core.setting(conn, "ops_maps_timeout",
                                                     project_id=core._project(conn, project)["id"])).total_seconds()
            r = core._run_command(cmd, str(work), limit, None, {**os.environ, "MAXPM_REV": commit})
            if getattr(r, "timed_out", False):
                err = f"the source map command stopped at its time limit (ops_maps_timeout {limit:.0f}s)"
            elif r.returncode != 0:
                err = f"the source map command ended with exit {r.returncode}: " + \
                      ((r.stderr or r.stdout or "").strip().splitlines() or [""])[-1][:300]
            else:
                files, n = {}, 0
                for m in sorted(work.rglob("*.map")):
                    built = m.with_suffix("")
                    if "node_modules" in m.parts or not built.is_file():
                        continue
                    shutil.copyfile(m, d / f"{n}.map")
                    files.setdefault(built.name, []).append({"map": f"{n}.map", "file": str(built.relative_to(work)),
                                                             "sha256": hashlib.sha256(built.read_bytes()).hexdigest()})
                    n += 1
                if not files:
                    err = "the source map command wrote no .map file beside a script"
                else:
                    out = {"rev": commit, "built_at": core.iso(core.now()), "files": files}
                    idx.write_text(json.dumps(out), encoding="utf-8")
                    failed.unlink(missing_ok=True)
                    return dict(out, note=note)
        failed.write_text(json.dumps({"error": err}), encoding="utf-8")
        return {"rev": commit, "error": err}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- the walk and its groups

def parse_pages(text):
    pages = [p for p in re.split(r"[\s,]+", text or "") if p]
    bad = [p for p in pages if not re.match(r"^https?://\S+$", p)]
    if not pages or bad:
        raise RiverError(f"bad --pages {text!r}: http:// or https:// addresses, separated by commas")
    return pages


def _walk(conn, project, pages, settle, timeout):
    from ..cli import ask_server
    return ask_server(conn, "browser", {"action": "walk", "project": project, "pages": pages, "settle": settle,
                                        "timeout": timeout}, None, timeout=timeout + 30, retries=1)


class _Mapper:
    def __init__(self, project, maps):
        self.project, self.maps, self.loaded = project, maps, {}

    def frame(self, f, scripts):
        """A frame with 1-based line and column, and its source position when the map is the build of the script."""
        out = dict(f, line=f["line"] + 1, column=f["column"] + 1)
        files = self.maps.get("files") or {}
        cands = files.get(posixpath.basename(urlsplit(f.get("url") or "").path)) if f.get("url") else None
        if not cands:
            return out
        served = scripts.get(f["url"])
        hit = next((c for c in cands if c["sha256"] == served), None)
        if hit is None:
            out["unmapped"] = (f"the script the browser ran is not the build of {self.maps['rev'][:12]} (its SHA-256 "
                               f"differs)" if served else "the browser gave no text of the script for the hash check")
            return out
        if hit["map"] not in self.loaded:
            try:
                self.loaded[hit["map"]] = SourceMap(json.loads((map_dir(self.project, self.maps["rev"]) / hit["map"])
                                                               .read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError) as e:
                self.loaded[hit["map"]] = None
                out["unmapped"] = f"the source map could not be read: {e}"
        m = self.loaded[hit["map"]]
        pos = m.lookup(f["line"], f["column"]) if m else None
        if pos:
            out.update(source=pos["source"], source_line=pos["line"], source_column=pos["column"],
                       name=pos["name"] or f.get("function") or "")
        return out


def frame_text(f):
    if f.get("source"):
        return f"at {f.get('name') or '<anonymous>'} ({f['source']}:{f['source_line']}:{f['source_column']})"
    where = f"{f['url']}:{f['line']}:{f['column']}" if f.get("url") else "<no script>"
    return f"at {f.get('function') or '<anonymous>'} ({where})" + (f"  [{f['unmapped']}]" if f.get("unmapped") else "")


def _shape(url):
    """An address as its group: no query, and numbers and ids in its path made general."""
    u = urlsplit(url or "")
    path = re.sub(r"(?<=/)(?:\d+|[0-9a-f]{8,}|[0-9a-f-]{36})(?=/|$)", "N", u.path, flags=re.I)
    return f"{u.scheme}://{u.netloc}{path}"


def _key(kind, message, frames, req):
    if req is not None:
        return f"request {req.get('status') or req.get('error')} {req.get('method', 'GET')} {_shape(req.get('url'))}"
    top = frames[0] if frames else {}
    where = f"{top['source']}:{top.get('name') or ''}" if top.get("source") else \
        f"{posixpath.basename(urlsplit(top.get('url') or '').path)}:{top.get('function') or ''}" if top else ""
    return f"{kind} {core._ops_group_text(message)} @ {where}"


def _noise():
    out = []
    for p in (os.environ.get("MAXPM_NOISE") or "").splitlines():
        try:
            out.append(re.compile(p))
        except re.error:
            pass
    return out


def run(name, project, pages, settle=3.0, timeout=60.0, rev=None):
    """Walk the pages, map the frames, and group what went wrong. Never raises: a failure is status 3."""
    res = {"name": name, "project": project, "status": 3, "summary": "", "groups": [], "pages": 0, "skipped": 0,
           "maps": None, "noise": 0}
    try:
        pages = parse_pages(pages) if isinstance(pages, str) else list(pages)
        conn = core.connect()
    except RiverError as e:
        res["summary"] = str(e)
        return res
    try:
        try:
            core._project(conn, project)
            walked = (WALK or _walk)(conn, project, pages, float(settle), float(timeout))
        except RiverError as e:
            res["summary"] = f"the walk did not run: {e}"
            return res
        done = [p for p in walked.get("pages") or [] if not p.get("skipped")]
        res["pages"], res["skipped"] = len(done), len(walked.get("pages") or []) - len(done)
        if not done:
            res["summary"] = f"no page was walked within the timeout {timeout:g}s"
            return res
        want = any(e["frames"] for p in done for e in p["errors"])
        maps = build_maps(conn, project, rev) if want else {"rev": None, "note": "no stack frame to map"}
        res["maps"] = {k: v for k, v in maps.items() if k != "files"}
        if maps.get("files"):
            res["maps"]["scripts"] = len(maps["files"])
        mapper, noise, groups = _Mapper(project, maps), _noise(), {}
        for p in done:
            found = [("load", f"the page did not load: {p['load']}", [], None)] if p.get("load") else []
            found += [(e["kind"], e["message"], [mapper.frame(f, p.get("scripts") or {}) for f in e["frames"]], None)
                      for e in p["errors"]]
            found += [("request", f"{q.get('method', 'GET')} {q.get('url')}: " +
                       (f"HTTP {q['status']}" if q.get("status") else str(q.get("error"))), [], q) for q in p["requests"]]
            for kind, message, frames, req in found:
                if any(n.search(message) for n in noise):
                    res["noise"] += 1
                    continue
                key = _key(kind, message, frames, req)
                g = groups.get(key)
                if g is None:
                    g = groups[key] = {"subject": f"{kind}-{hashlib.sha1(key.encode()).hexdigest()[:8]}", "kind": kind,
                                       "message": message, "frames": frames, "pages": [], "count": 0}
                g["count"] += 1
                if p["url"] not in g["pages"]:
                    g["pages"].append(p["url"])
        res["groups"] = sorted(groups.values(), key=lambda g: -g["count"])  # ties: in the order the walk met them
        hit = len({u for g in res["groups"] for u in g["pages"]})
        n = len(res["groups"])
        res["status"] = 2 if n else 0
        res["summary"] = (f"{n} error group{'s' if n != 1 else ''} on {hit} of {len(done)} page(s): "
                          + "; ".join(g["message"][:80] for g in res["groups"][:3]) if n else
                          f"no browser error on {len(done)} page(s)") + \
            (f"; {res['skipped']} page(s) skipped at the timeout" if res["skipped"] else "")
        return res
    finally:
        conn.close()


def lines(res):
    """The plugin's output: the status line, a line and its frames for each group, and the evidence object."""
    out = [f"PAGES {STATUS[res['status']]} - {res['summary']} | groups={len(res['groups'])} pages={res['pages']}"]
    for g in res["groups"]:
        pages = ", ".join(urlsplit(u).path or "/" for u in g["pages"][:3]) + (" ..." if len(g["pages"]) > 3 else "")
        out.append(f"{g['subject']}: CRITICAL {g['message'][:300]} ({g['count']}x on {pages})")
        out += ["  " + frame_text(f) for f in g["frames"][:FRAMES_SHOWN]]
    m = res.get("maps") or {}
    if m.get("error"):
        out.append(f"  source maps not used: {m['error']}")
    elif m.get("note") and m.get("rev"):
        out.append(f"  source maps of {m['rev'][:12]}: {m['note']}")
    out.append(json.dumps({"groups": res["groups"][:GROUPS_KEPT], "maps": res.get("maps"), "pages": res["pages"],
                           "skipped": res["skipped"], "noise": res["noise"]}))
    return out
