"""MaximizePM learns of a new release and updates itself (#5601).

Once a day maxpm serve asks GitHub for the latest release (check, a step of the loop of serve) and keeps the answer in
release.json beside the queue, so the page and the briefings read it with no network call (status). A newer version
shows a bar on the page with its notes and one button. What the button does depends on the install (install_kind):

- app: the desktop app. The app keeps the MaximizePM code in a folder of the user's (app_dir()/versions/<version>),
  next to the copy inside the .app, and starts the newest good one (desktop/main.js). Update downloads the release's
  code archive (named in the release's manifest.json, with its sha256), checks it, unpacks it beside the old version,
  checks that it loads, makes it the current version, and starts serve again on it (apply_code). The .app itself does
  not change, so macOS shows no download or Gatekeeper step. Only when the release needs a new app shell (Electron, the
  app's own files or its Python changed: the manifest's shell id differs from MAXPM_APP_SHELL) does the bar offer the
  new .dmg, which MaximizePM downloads and opens (download_shell).
- git: a clone. The button runs the git Update of the page (server.update_apply).
- other: pip or a copy; the bar says to update it the way it was installed.
"""

import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from . import __version__, core
from .core import RiverError

SLUG = "mhalstrom/maximizepm"
LATEST = f"https://api.github.com/repos/{SLUG}/releases/latest"
RELEASES = f"https://github.com/{SLUG}/releases"
FILE = "release.json"  # beside the queue
MANIFEST = "manifest.json"  # a release asset: version, code archive and its sha256, shell id (desktop-release.yml)
EVERY = 24 * 3600  # a check a day
AFTER_ERROR = 3600  # a failed check tries again after an hour
FETCH = None  # tests: a function (url, timeout) -> bytes in place of the network
REPO = Path(__file__).resolve().parent.parent


def version_tuple(v):
    """(1, 2, 3) for "v1.2.3" or "1.2.3", else None."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newer(a, b):
    """True when version a is newer than version b."""
    ta, tb = version_tuple(a), version_tuple(b)
    return bool(ta and tb and ta > tb)


def install_kind():
    """app (the desktop app: it tells serve where its bundle is), git (a clone), or other."""
    if os.environ.get("MAXPM_DESKTOP") == "1":
        return "app" if os.environ.get("MAXPM_APP_BUNDLE") else "other"
    return "git" if (REPO / ".git").exists() else "other"


def app_dir():
    """Where the desktop app keeps its versions of the code: versions/<version>, and the file current, which names
    the folder that the app and the maxpm launcher run."""
    return Path(os.environ.get("MAXPM_APP_DIR") or "~/.maximizepm/app").expanduser()


def _get(url, timeout=20):
    if FETCH is not None:
        return FETCH(url, timeout)
    import urllib.request
    headers = {"User-Agent": f"MaximizePM/{__version__}"}
    if url.startswith("https://api.github.com/"):
        headers["Accept"] = "application/vnd.github+json"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read()


def notes(body):
    """What is new, from a release's notes: the text before its Download part (how to install is not news)."""
    text = re.split(r"\n##\s+Download\b", (body or "").replace("\r\n", "\n"), maxsplit=1)[0].strip()
    return text[:6000]


def read(conn):
    """The last check, as check wrote it: {} before the first."""
    p = core._beside_queue(conn, FILE)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p else {}
    except (OSError, ValueError):
        return {}


def _write(conn, data):
    p = core._beside_queue(conn, FILE)
    if p is None:
        return
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, p)


def check(conn, force=False):
    """Ask GitHub for the latest release, at most once a day (an hour after a failed check), and keep the answer.
    A step of the loop of maxpm serve; maxpm update --check (force) asks at once. Returns what read returns."""
    old = read(conn)
    if core.setting(conn, "update_check") != "on" and not force:
        return old
    wait = AFTER_ERROR if old.get("error") else EVERY
    if not force and time.time() - float(old.get("checked_at") or 0) < wait:
        return old
    out = {"checked_at": time.time(), "error": None, "latest": old.get("latest"), "manifest": old.get("manifest")}
    try:
        r = json.loads(_get(LATEST))
        tag = r.get("tag_name") or ""
        if not version_tuple(tag):
            raise ValueError(f"the latest release has no version tag: {tag!r}")
        assets = {a["name"]: a["browser_download_url"] for a in r.get("assets") or []
                  if a.get("name") and a.get("browser_download_url")}
        latest = {"tag": tag, "version": tag.lstrip("v"), "notes": notes(r.get("body")),
                  "url": r.get("html_url") or f"{RELEASES}/tag/{tag}", "published_at": r.get("published_at"),
                  "assets": assets}
        man = None
        if newer(latest["version"], __version__) and MANIFEST in assets:
            man = json.loads(_get(assets[MANIFEST]))
        out.update(latest=latest, manifest=man)
    except Exception as e:  # noqa: BLE001  (no network, a rate limit, a bad answer: the next check tries again)
        out["error"] = f"{type(e).__name__}: {e}"
    _write(conn, out)
    return out


def loop_step(conn):
    """The step of the loop of maxpm serve."""
    check(conn)


def _arch():
    return "arm64" if platform.machine() in ("arm64", "aarch64") else "x64"


def status(conn):
    """What the page and the briefings show, from the last check, with no network call:
    current, newer, version, notes, url, kind (app, git, other), and how the update runs:
    code (the app swaps its code), shell (the app needs its new .dmg), git (the git Update), other (by hand)."""
    c, kind = read(conn), install_kind()
    out = {"current": __version__, "kind": kind, "checked_at": c.get("checked_at"), "error": c.get("error"),
           "check": core.setting(conn, "update_check"), "newer": False}
    latest = c.get("latest") or {}
    if not newer(latest.get("version"), __version__):
        return out
    man = c.get("manifest") or {}
    how = kind
    if kind == "app":
        same_shell = man.get("shell") and man.get("shell") == os.environ.get("MAXPM_APP_SHELL")
        how = "code" if same_shell and man.get("code") else "shell"
    return {**out, "newer": True, "version": latest["version"], "tag": latest.get("tag"),
            "notes": latest.get("notes") or "", "url": latest.get("url") or RELEASES, "how": how,
            "dmg": f"MaximizePM-{latest['version']}-{_arch()}.dmg" if how == "shell" else None}


def notice(conn):
    """One line for a briefing when a newer version is out, else None."""
    st = status(conn)
    if not st["newer"]:
        return None
    return (f"MaximizePM {st['version']} is out (this computer runs {st['current']}): the person updates it with "
            f"Update in the bar of the page. Notes: {st['url']}")


def _safe_members(t):
    """The members of a code archive, refused when one would land outside its folder or is a link."""
    names = []
    for m in t.getmembers():
        parts = Path(m.name).parts
        if m.name.startswith("/") or ".." in parts or m.issym() or m.islnk() or not (m.isfile() or m.isdir()):
            raise RiverError(f"the code archive holds {m.name!r}, which MaximizePM does not unpack")
        names.append(m.name)
    tops = {Path(n).parts[0] for n in names if Path(n).parts}
    if len(tops) != 1:
        raise RiverError("the code archive must hold one folder")
    return tops.pop()


def start_check(root, version):
    """None when the code in root has the maxpm command, loads in a new Python, and says it is version; else why."""
    if not (Path(root) / "bin" / "maxpm").is_file():
        return "it has no bin/maxpm"
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import river, river.server, river.cli, river.notify, "
            "river.mcp, river.loop; print(river.__version__)")
    try:
        r = subprocess.run([sys.executable, "-c", code, str(root)], cwd=str(root), capture_output=True, text=True,
                           timeout=120, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    if r.returncode:
        return ((r.stderr or r.stdout).strip().splitlines() or ["it does not load"])[-1]
    got = r.stdout.strip()
    return None if got == version else f"it says it is version {got or '?'}, not {version}"


def install_code(data, version):
    """Unpack a code archive into app_dir()/versions/<version> after its start check. Returns the folder."""
    base = app_dir() / "versions"
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{version}-", dir=base))
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            top = _safe_members(t)
            kw = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            t.extractall(tmp, **kw)
        src = tmp / top
        why = start_check(src, version)
        if why:
            raise RiverError(f"version {version} did not pass its start check, so MaximizePM stays on "
                             f"{__version__}: {why}")
        dest = base / version
        if dest.exists():  # an earlier copy that failed at its start: this one passed the check
            shutil.rmtree(dest)
        os.replace(src, dest)
        return dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def set_current(root):
    """Make root the code that the app starts and the maxpm launcher runs. A new serve clears trying when it
    answers; when it does not, the app finds trying at its next start and goes back to the version before."""
    d = app_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name in ("current", "trying"):
        tmp = d / f".{name}.tmp"
        tmp.write_text(str(root) + "\n", encoding="utf-8")
        os.replace(tmp, d / name)


def started():
    """A serve that runs from the folder that an update is trying now: that version starts, so clear trying."""
    p = app_dir() / "trying"
    try:
        if Path(p.read_text(encoding="utf-8").strip()).resolve() == REPO.resolve():
            p.unlink()
    except OSError:
        pass


def prune(keep):
    """Remove the app's old copies of the code: keep the new one and the one that runs now (the way back)."""
    base = app_dir() / "versions"
    keep = {Path(k).resolve() for k in keep}
    for d in base.iterdir() if base.is_dir() else ():
        if d.is_dir() and not d.name.startswith(".") and d.resolve() not in keep:
            shutil.rmtree(d, ignore_errors=True)


def apply_code(conn, restart):
    """The app's Update: download the new code, check it, switch to it, and start serve again on it (restart(root),
    after the reply goes out). On any failure MaximizePM stays on the version it runs and says why."""
    if install_kind() != "app":
        raise RiverError("only the desktop app updates its code this way; a clone uses the git Update")
    st = status(conn)
    if not st["newer"]:
        return {"updated": False, "current": __version__}
    if st["how"] != "code":
        raise RiverError(f"MaximizePM {st['version']} needs the new app: use Download the new app")
    c = read(conn)
    man, assets = c.get("manifest") or {}, (c.get("latest") or {}).get("assets") or {}
    code = man.get("code") or {}
    url = assets.get(code.get("name") or "")
    if not url or not code.get("sha256") or man.get("version") != st["version"]:
        raise RiverError(f"the release {st['tag']} has no code archive that matches its manifest; "
                         f"download the new app from {st['url']}")
    try:
        data = _get(url, timeout=300)
    except Exception as e:  # noqa: BLE001
        raise RiverError(f"could not download MaximizePM {st['version']}, so it stays on {__version__}: {e}")
    if hashlib.sha256(data).hexdigest() != code["sha256"]:
        raise RiverError(f"the download of MaximizePM {st['version']} does not match its checksum, so MaximizePM "
                         f"stays on {__version__}")
    root = install_code(data, st["version"])
    set_current(root)
    prune([root, REPO])
    restart(root)
    return {"updated": True, "from": __version__, "to": st["version"], "root": str(root)}


def download_shell(conn, opener=None):
    """The new .dmg for this Mac's chip: download it to ~/Downloads and open it, so the person drags the new app to
    Applications. For a release that needs a new app shell."""
    st = status(conn)
    if not st["newer"] or st["how"] != "shell":
        raise RiverError("no new app to download; Update changes the code in place")
    url = ((read(conn).get("latest") or {}).get("assets") or {}).get(st["dmg"])
    if not url:
        raise RiverError(f"the release has no {st['dmg']}; download it from {st['url']}")
    try:
        data = _get(url, timeout=900)
    except Exception as e:  # noqa: BLE001
        raise RiverError(f"could not download {st['dmg']}: {e}")
    dest = Path("~/Downloads").expanduser() / st["dmg"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    (opener or (lambda p: subprocess.Popen(["open", str(p)])))(dest)
    return {"path": str(dest), "version": st["version"]}
