"""Plugin install, versions and trust (#6166; canvas release-tracking, 'Any deployment process, and plugins', Decision 11).

mark 2026-11-18: plugins first came as examples; installing, versions and trust follow. A plugin is a folder (plugin.py
names the contract). This module:

- installs one: maxpm plugin add <folder or git address> [--ref <tag>] [--path <subfolder>] copies its files into the
  plugins folder beside the queue (~/.maximizepm/plugins/<name>, the folder plugin.find reads) and keeps a copy of
  each version (plugins/.versions/<name>/<key>, a row of plugin_versions). Nothing goes into an agent CLI's home
  (policy p140);
- pins a version for each pipeline: maxpm pipeline forge <p> <plugin> pins the version installed then, and
  maxpm plugin pin <pipeline> <version> moves it; a pipeline with no pin runs the plugin's folder (one a person is
  writing: maxpm plugin new);
- asks a person's yes: a plugin runs only when a person approved its exact files (their sha256, the digest). Its
  manifest (level, actions, secrets, settings, hosts, the command) is what the person reads: maxpm plugin trust <name>
  shows it and records the yes. A pipeline whose plugin has no yes (new, or its files changed since) does not poll:
  the read says why, and a person's item asks once for each digest; done by a person, it is the yes. The plugins that
  MaximizePM ships need no yes: they are its own code.

The digest covers every file of the folder but var/ (a plugin writes its own files there, if any: MaximizePM never
copies or approves them), __pycache__, .git, *.pyc and .DS_Store.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

from .. import core
from . import plugin, store

VERSIONS = ".versions"
SKIP_DIRS = {"var", "__pycache__", ".git"}
SKIP_FILES = {".DS_Store"}
FILES_MAX = 2000
BYTES_MAX = 20 * 1024 * 1024
EXAMPLES = ("jsonl", "github", "gitlab", "pulumi")  # the shipped plugins that maxpm plugin new starts from
_DIGESTS = {}  # folder -> (stat signature, digest)


# ---------------------------------------------------------------- files and digests

def user_dir():
    """The plugins folder beside the queue (the last of plugin.dirs())."""
    return plugin.dirs()[-1]


def shipped(m):
    """Whether manifest m is of a plugin that MaximizePM ships (river/pipeline/plugins)."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")
    return os.path.dirname(os.path.abspath(m["folder"]).rstrip(os.sep)) == here


def files(folder):
    """[(relative path, absolute path)] of a plugin's files, sorted; refuses a folder that is too large."""
    out, size = [], 0
    for root, ds, fs in os.walk(folder):
        ds[:] = sorted(d for d in ds if not (root == folder and d == "var") and d not in SKIP_DIRS - {"var"})
        for f in sorted(fs):
            if f in SKIP_FILES or f.endswith(".pyc"):
                continue
            path = os.path.join(root, f)
            if os.path.islink(path):
                raise core.RiverError(f"the plugin in {folder} holds a link ({os.path.relpath(path, folder)}); copy "
                                      "the file itself into the plugin")
            out.append((os.path.relpath(path, folder).replace(os.sep, "/"), path))
            size += os.path.getsize(path)
    if len(out) > FILES_MAX or size > BYTES_MAX:
        raise core.RiverError(f"the plugin in {folder} has {len(out)} files and {size // 1024} KB; a plugin has at most "
                              f"{FILES_MAX} files and {BYTES_MAX // (1024 * 1024)} MB")
    return out


def digest(folder):
    """sha256 of a plugin's files (their paths and bytes). Read again only when a file's size or time changed."""
    fl = files(folder)
    sig = tuple((rel, os.stat(p).st_mtime_ns, os.stat(p).st_size) for rel, p in fl)
    hit = _DIGESTS.get(folder)
    if hit and hit[0] == sig:
        return hit[1]
    h = hashlib.sha256()
    for rel, p in fl:
        h.update(rel.encode() + b"\0")
        with open(p, "rb") as f:
            h.update(hashlib.sha256(f.read()).digest())
    _DIGESTS[folder] = (sig, h.hexdigest())
    return h.hexdigest()


def _copy(src, dst):
    """Copy a plugin's files (files()) into a new folder dst, keeping the mode of each (an executable stays one)."""
    for rel, p in files(src):
        out = os.path.join(dst, *rel.split("/"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copy2(p, out)


def summary(m):
    """What a person approves: the manifest's parts that say what the plugin does and reaches."""
    return {"name": m["name"], "version": m["version"], "level": m["level"], "actions": m["actions"],
            "secrets": {k: (d.get("help") or "") for k, d in m["secrets"].items()},
            "settings": {k: (d.get("help") or "") for k, d in m["settings"].items()},
            "hosts": m.get("hosts") or [], "run": m["run"], "description": m["description"]}


def summary_text(s):
    return "\n".join([
        f"plugin {s['name']} {s['version']}: {s['description']}",
        f"level: {s['level']}" + (f"; actions: {', '.join(s['actions'])}" if s["actions"] else " (it only reads)"),
        "secrets it gets: " + (", ".join(f"{k} ({v})" for k, v in s["secrets"].items()) or "none"),
        "settings: " + (", ".join(f"{k} ({v})" for k, v in s["settings"].items()) or "none"),
        "hosts it reaches: " + (", ".join(s["hosts"]) or "none named"),
        "command: " + " ".join(s["run"])])


# ---------------------------------------------------------------- trust

def _lead(conn, actor, what):
    why = core._lead_refusal(conn, actor, what)
    if why:
        raise core.RiverError(why)


def _person(conn, actor):
    if actor is not None and not core._is_person(conn, actor):
        raise core.RiverError(f"only a person approves a plugin (its code runs on this computer with the secrets its "
                              f"pipelines map to it); {actor} is not a person. Ask the person: the plugin's item, or "
                              "maxpm plugin trust <name> in their terminal")


def _row(conn, name, dg):
    return conn.execute("SELECT * FROM plugin_trust WHERE name=? AND digest=?", (name, dg)).fetchone()


def state(conn, m):
    """{trusted, digest, by, at, item}: whether a person approved these files of the plugin."""
    if shipped(m):
        return {"trusted": True, "digest": None, "by": "MaximizePM", "at": None, "item": None, "shipped": True}
    dg = digest(m["folder"])
    r = _row(conn, m["name"], dg)
    if r and not r["trusted_by"] and r["item"]:
        it = conn.execute("SELECT status FROM items WHERE id=?", (r["item"],)).fetchone()
        by = core._done_by(conn, r["item"]) if it and it["status"] == "done" else None
        if by and core._is_person(conn, by):  # the person's yes on the item
            with core.tx(conn):
                conn.execute("UPDATE plugin_trust SET trusted_by=?, trusted_at=? WHERE id=?",
                             (by, core.iso(core.now()), r["id"]))
            r = _row(conn, m["name"], dg)
    return {"trusted": bool(r and r["trusted_by"]), "digest": dg, "by": r["trusted_by"] if r else None,
            "at": r["trusted_at"] if r else None, "item": r["item"] if r else None, "shipped": False}


def _last_yes(conn, name):
    return conn.execute("SELECT version, trusted_by FROM plugin_trust WHERE name=? AND trusted_by IS NOT NULL ORDER BY "
                        "trusted_at DESC LIMIT 1", (name,)).fetchone()


def check(conn, m, p=None):
    """Refuse to run plugin m when a person has not approved its files. With pipeline p (a read of maxpm serve), a
    person's item asks once for this digest, in the pipeline's project."""
    st = state(conn, m)
    if st["trusted"]:
        return st
    last = _last_yes(conn, m["name"])
    what = (f"its files changed since {last['trusted_by']} approved {m['name']} {last['version']}" if last
            else "no person approved it yet")
    item = st["item"]
    if item is None and p is not None and p["project"]:
        s = summary(m)
        new = core.item_add(
            conn, p["project"], f"Approve plugin {m['name']} {m['version']} for pipeline {p['name']}", doer="human",
            actor="maxpm", short=f"Approve plugin {m['name']}",
            context=(f"Pipeline {p['name']} reads its system through plugin {m['name']}: {what}. It runs on this "
                     f"computer with the secrets the pipeline maps to it. What it says it does:\n{summary_text(s)}\n"
                     f"Files: {m['folder']} (digest {st['digest'][:16]}).\nApprove: mark this item done, or run maxpm "
                     f"plugin trust {m['name']}. Not this one: drop it; the pipeline stays without news."))
        item = new["id"]
        with core.tx(conn):
            conn.execute("INSERT OR IGNORE INTO plugin_trust(name, digest, version, manifest, asked_at) VALUES "
                         "(?,?,?,?,?)", (m["name"], st["digest"], m["version"], json.dumps(s), core.iso(core.now())))
            conn.execute("UPDATE plugin_trust SET item=? WHERE name=? AND digest=?", (item, m["name"], st["digest"]))
    raise core.RiverError(f"plugin {m['name']} {m['version']} waits for a person's yes ({what}): a person reads "
                          f"its manifest and approves it: maxpm plugin trust {m['name']}"
                          + (f", or marks #{item} done" if item else ""))


def trust(conn, name, version=None, actor=None):
    """maxpm plugin trust <name> [--version <key>]: a person's yes for the files of the plugin's folder (or of a kept
    version): what it does, the secrets it gets and the hosts it reaches, as its manifest says."""
    _person(conn, actor)
    m = _find(conn, name, version)
    s = summary(m)
    if shipped(m):
        return {"name": name, "shipped": True, "summary": s, "text": summary_text(s)}
    dg = digest(m["folder"])
    at = core.iso(core.now())
    with core.tx(conn):
        conn.execute("INSERT OR IGNORE INTO plugin_trust(name, digest, version, manifest) VALUES (?,?,?,?)",
                     (name, dg, m["version"], json.dumps(s)))
        conn.execute("UPDATE plugin_trust SET trusted_by=?, trusted_at=?, version=?, manifest=? WHERE name=? AND "
                     "digest=?", (actor or "a person", at, m["version"], json.dumps(s), name, dg))
        r = _row(conn, name, dg)
    if r["item"]:
        it = conn.execute("SELECT status FROM items WHERE id=?", (r["item"],)).fetchone()
        if it and it["status"] not in core.CLOSED_STATES:
            core.done(conn, r["item"], output=f"approved with maxpm plugin trust {name}", actor=actor)
    return {"name": name, "version": m["version"], "digest": dg, "trusted_by": actor or "a person", "summary": s,
            "text": summary_text(s), "folder": m["folder"]}


# ---------------------------------------------------------------- versions

def _versions(conn, name):
    return [dict(r) for r in conn.execute("SELECT * FROM plugin_versions WHERE name=? ORDER BY id", (name,))]


def version_folder(name, key):
    """plugins/.versions/<name>/<key>/<name>: the last folder has the plugin's name, as its manifest must."""
    return os.path.join(user_dir(), VERSIONS, name, key, name)


def _find(conn, name, key=None):
    """The manifest of the plugin's folder, or of its kept version key (a version string takes its newest copy)."""
    if not key:
        return plugin.find(name)
    r = conn.execute("SELECT key FROM plugin_versions WHERE name=? AND (key=? OR version=?) ORDER BY id DESC LIMIT 1",
                     (name, key, key)).fetchone()
    if not r:
        raise core.RiverError(f"plugin {name} keeps no version {key}; maxpm plugin show {name} lists them")
    return plugin.find(name, pin=r["key"])


def _git(source, ref, into):
    argv = ["git", "clone", "--quiet", "--depth", "1"] + (["--branch", ref] if ref else []) + ["--", source, into]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        raise core.RiverError(f"git clone {source} did not run: {e}") from None
    if r.returncode != 0:
        raise core.RiverError(f"git clone {source} failed: {(r.stderr or r.stdout).strip()[-500:]}")


def _is_git(source):
    return bool(re.match(r"^(https?|ssh|git|file)://", source) or re.match(r"^[\w.-]+@[\w.-]+:", source)
                or source.endswith(".git"))


def add(conn, source, ref=None, path=None, replace=False, actor=None):
    """maxpm plugin add <folder or git address>: install the plugin (a person or the manager). It keeps this version
    and makes it the plugin's folder; pipelines pinned to another version keep theirs. A plugin runs only after a
    person's yes for its files (trust)."""
    _lead(conn, actor, "installs a plugin")
    source = (source or "").strip()
    if not source:
        raise core.RiverError("name the plugin: maxpm plugin add <folder or git address>")
    with tempfile.TemporaryDirectory(prefix="maxpm-plugin-") as tmp:
        if _is_git(source):
            _git(source, ref, os.path.join(tmp, "clone"))
            src = os.path.join(tmp, "clone")
        else:
            if ref:
                raise core.RiverError("--ref is for a git address; a folder is copied as it is")
            src = os.path.abspath(os.path.expanduser(source))
            if not os.path.isdir(src):
                raise core.RiverError(f"no folder {source}")
        if path:
            src = os.path.normpath(os.path.join(src, path))
        try:
            with open(os.path.join(src, plugin.MANIFEST), encoding="utf-8") as f:
                raw = json.load(f)
        except OSError:
            raise core.RiverError(f"{source}{'/' + path if path else ''} has no {plugin.MANIFEST}"
                                  + ("; name its folder with --path" if not path else "")) from None
        except ValueError as e:
            raise core.RiverError(f"{plugin.MANIFEST} of {source} is not JSON ({e})") from None
        name = raw.get("name") if isinstance(raw, dict) else None
        if not isinstance(name, str) or not plugin._NAME.match(name):
            raise core.RiverError(f"{plugin.MANIFEST} of {source} names no usable plugin name")
        stage = os.path.join(tmp, "stage", name)
        _copy(src, stage)
        m = plugin._check(raw, stage)  # the whole manifest, before anything is installed
        if plugin.find(name, need=False) is not None and shipped(plugin.find(name, need=False)):
            raise core.RiverError(f"MaximizePM ships a plugin {name}; name yours another way")
        dg = digest(stage)
        keep = conn.execute("SELECT key FROM plugin_versions WHERE name=? AND digest=?", (name, dg)).fetchone()
        key = keep["key"] if keep else m["version"]
        if not keep and conn.execute("SELECT 1 FROM plugin_versions WHERE name=? AND key=?", (name, key)).fetchone():
            key = f"{m['version']}+{dg[:8]}"  # the same version number with other files: both stay
        live = os.path.join(user_dir(), name)
        if os.path.isdir(live) and not replace:
            known = {r["digest"] for r in conn.execute("SELECT digest FROM plugin_versions WHERE name=?", (name,))}
            if digest(live) not in known:
                raise core.RiverError(f"{live} holds files that no kept version of {name} has (a plugin you write?); "
                                      "move them away, or add --replace to put the new version in their place")
        vdir = version_folder(name, key)
        if not os.path.isdir(vdir):
            _copy(stage, vdir)
        tmp_live = live + ".new"
        shutil.rmtree(tmp_live, ignore_errors=True)
        _copy(stage, tmp_live)
        if os.path.isdir(live):
            shutil.rmtree(live)
        os.replace(tmp_live, live)
    plugin._CACHE.clear()
    src_text = source + (f" --ref {ref}" if ref else "") + (f" --path {path}" if path else "")
    with core.tx(conn):
        if not keep:
            conn.execute("INSERT INTO plugin_versions(name, key, version, digest, source, added_by, added_at) VALUES "
                         "(?,?,?,?,?,?,?)", (name, key, m["version"], dg, src_text, actor, core.iso(core.now())))
    m = plugin.find(name)
    return {"name": name, "version": m["version"], "key": key, "kept": bool(keep), "folder": live,
            "trust": state(conn, m), "summary": summary(m), "text": summary_text(summary(m)),
            "pinned": _pins(conn, name)}


def new(conn, name, example="jsonl", actor=None):
    """maxpm plugin new <name> [--from <example>]: a starter plugin in the plugins folder, copied from a shipped
    plugin with its name and version 0.1.0. Its author changes it; a person approves it before its first run."""
    if not plugin._NAME.match(name or "") or name in plugin.RESERVED:
        raise core.RiverError("a plugin's name is lower case letters, digits and '-' (at most 40), not one of "
                              f"{', '.join(sorted(plugin.RESERVED))}")
    if example not in EXAMPLES:
        raise core.RiverError(f"start from one of {', '.join(EXAMPLES)}")
    if plugin.find(name, need=False) is not None or os.path.exists(os.path.join(user_dir(), name)):
        raise core.RiverError(f"a plugin {name} exists already: maxpm plugin show {name}")
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins", example)
    dst = os.path.join(user_dir(), name)
    _copy(src, dst)
    path = os.path.join(dst, plugin.MANIFEST)
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    m.update(name=name, version="0.1.0",
             description=f"{name}: a plugin started from the example {example}. Say here what system it reads.")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)
        f.write("\n")
    plugin._CACHE.clear()
    return {"name": name, "folder": dst, "example": example,
            "next": [f"change {dst}/plugin.json (its settings, secrets, hosts) and its program",
                     f"test it: maxpm pipeline plugin {name} (after the yes)",
                     f"a person approves it: maxpm plugin trust {name}",
                     f"use it: maxpm pipeline forge <pipeline> {name} --set <key>=<value> --secret <name>=<secret>"]}


def _pins(conn, name):
    return [{"pipeline": r["name"], "pin": r["plugin_pin"]} for r in conn.execute(
        "SELECT name, plugin_pin FROM pipelines WHERE forge=? ORDER BY name", (name,))]


def pin_for_use(conn, name):
    """The kept version that a pipeline pins when it starts to use the plugin: the one whose files the plugin's folder
    holds now; None when the folder holds files no version keeps (a plugin being written)."""
    m = plugin.find(name, need=False)
    if m is None or shipped(m):
        return None
    r = conn.execute("SELECT key FROM plugin_versions WHERE name=? AND digest=? ORDER BY id DESC LIMIT 1",
                     (name, digest(m["folder"]))).fetchone()
    return r["key"] if r else None


def pin(conn, pipeline, key, actor=None):
    """maxpm plugin pin <pipeline> <version|folder>: the kept version of its plugin that the pipeline runs; folder: the
    plugin's folder as it is now (a plugin being written). A person or the manager."""
    from . import define
    define._may(conn, actor)
    p = store._pipeline(conn, pipeline)
    name = plugin.plugin_name(p["forge"]) if p["forge"] else None
    m = plugin.find(name, need=False) if name else None
    if not name or name == "none" or (m is not None and shipped(m)) or (m is None and not _versions(conn, name)):
        raise core.RiverError(f"pipeline {p['name']} uses no installed plugin of its own (its source: "
                              f"{p['forge'] or 'this computer'}); a shipped plugin has one version, MaximizePM's")
    if key in ("folder", "none", ""):
        new_pin = None
    else:
        r = conn.execute("SELECT key FROM plugin_versions WHERE name=? AND (key=? OR version=?) ORDER BY id DESC "
                         "LIMIT 1", (name, key, key)).fetchone()
        if not r:
            raise core.RiverError(f"plugin {name} keeps no version {key}; kept: "
                                  f"{', '.join(v['key'] for v in _versions(conn, name)) or 'none'}")
        new_pin = r["key"]
    with core.tx(conn):
        conn.execute("UPDATE pipelines SET plugin_pin=? WHERE id=?", (new_pin, p["id"]))
        define._change(conn, p["id"], actor, f"plugin {name} pinned at {new_pin or 'its folder'} (was "
                                             f"{p['plugin_pin'] or 'its folder'})")
    got = plugin.find(name, pin=new_pin)
    return {"pipeline": p["name"], "plugin": name, "pin": new_pin, "version": got["version"],
            "trust": state(conn, got)}


# ---------------------------------------------------------------- reads

def listing(conn):
    """maxpm plugin list: each plugin, shipped or installed, with its version, its yes and the pipelines that use it."""
    out = []
    for row in plugin.listing():
        if "error" not in row:
            m = plugin.find(row["name"], need=False)
            if m is not None and os.path.abspath(m["folder"]) == os.path.abspath(row["folder"]):
                st = state(conn, m)
                row["trusted"], row["trusted_by"], row["shipped"] = st["trusted"], st["by"], st["shipped"]
        row["versions"] = [v["key"] for v in _versions(conn, row["name"])]
        row["pipelines"] = _pins(conn, row["name"])
        out.append(row)
    return out


def show(conn, name):
    """maxpm plugin show <name>: its manifest as a person approves it, its yes, its kept versions and each one's yes,
    and the pipelines that use it with their pins."""
    m = plugin.find(name)
    st = state(conn, m)
    vers = []
    for v in _versions(conn, name):
        t = _row(conn, name, v["digest"])
        vers.append(dict(v, trusted_by=t["trusted_by"] if t else None, live=st["digest"] == v["digest"]))
    s = summary(m)
    return {"name": name, "folder": m["folder"], "summary": s, "text": summary_text(s), "trust": st,
            "versions": vers, "pipelines": _pins(conn, name)}
