"""Pipeline plugins (#6159; Decision 11 of canvas release-tracking, mark 2026-11-18: A, an executable in any
language with a manifest, JSON over stdin and stdout).

A plugin connects a pipeline to a release system that MaximizePM does not know (Jenkins, Argo CD, a cron job). It
is a folder with a manifest, plugin.json, and an executable in any language. It imports none of our code:
maxpm serve runs it as a process for each call, writes one JSON object (the request) on its stdin and reads one
JSON object (the answer) from its stdout. The calls:

- describe: the plugin says its name, version, level and actions (they must match its manifest);
- poll: the plugin reads its system and answers with CDEvents events (store.ingest takes them, as it takes the
  events of the built-in adapters); maxpm serve calls it every poll_every seconds for each pipeline that uses it;
- one call for each action it lists: start (a stage's gate opened), rerun, rollback, stop.

The levels: report (poll only), act (poll and the actions it lists), gate (as act; the outside flow also asks our
gates before it moves on, a later task). The page shows an action that the plugin does not list as 'not available
for this source', and the command that asks for it is refused.

Keys: the manifest names the secrets the plugin needs; the pipeline maps each one to a maxpm secret (maxpm pipeline
forge <p> <plugin> --secret token=<secret name>), and the request carries the values. A plugin gets no other secret
and no variable of maxpm serve's environment but PATH, HOME, the language and the temporary folder.

A crash, a hang or bad output cannot stop maxpm serve: each call has a time limit (the manifest's timeout, at most
TIMEOUT_MAX), runs in a process group of its own that is stopped at the limit, and its output is cut at OUT_MAX. A
failed poll stays on the pipeline (poll_error); a failed start fails the stage run with the plugin's message.

Where plugins live: river/pipeline/plugins/<name>/ (shipped with MaximizePM) and plugins/<name>/ beside the queue's
database (~/.maximizepm/plugins). A shipped plugin wins over the user's of the same name. The reference for plugin
authors is maxpm guide pipelines (skills/maxpm/PIPELINES.md), section 'Write a plugin'.

RUNNER is a test hook: fn(argv, folder, request dict, timeout) -> (exit code, stdout bytes, stderr text).
"""

import json
import os
import re
import subprocess
import sys
import threading
import time

from .. import core
from . import store

CONTRACT = 1  # the version of the contract; a manifest names the one it follows
MANIFEST = "plugin.json"
LEVELS = ("report", "act", "gate")
ACTIONS = ("start", "stop", "rerun", "rollback")  # the calls of a plugin besides describe and poll
ALL_ACTIONS = list(ACTIONS)
# The built-in adapters (runner.FORGES): what each one can do. Pulumi Delivery has no roll back by its API (#6133).
BUILTIN_ACTIONS = {"github": ALL_ACTIONS, "gitlab": ALL_ACTIONS, "gitea": ALL_ACTIONS, "forgejo": ALL_ACTIONS,
                   "pulumi": ["start", "stop", "rerun"]}
RESERVED = set(BUILTIN_ACTIONS) | {"none", "maxpm", "here", "plugin"}
POLL_EVERY = 60  # seconds between two polls of one pipeline, when the manifest names no poll_every
POLL_MIN = 10
TIMEOUT = 30  # seconds for one call, when the manifest names no timeout
TIMEOUT_MAX = 300
OUT_MAX = 8 * 1024 * 1024  # bytes of an answer
ERR_KEEP = 2000  # characters of stderr kept for an error
EVENTS_MAX = 2000  # events in one answer
STATE_MAX = 64 * 1024  # bytes of the state a plugin keeps between calls
TOKEN_KEEP = 600  # seconds a secret's value stays in memory (each read from the store is a line of its log)
KEEP_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ")
RUNNER = None
DIRS = None  # tests: the folders to look in, instead of dirs()
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_CACHE = {}  # manifest path -> (mtime, manifest or error)
_SECRETS = {}


def dirs():
    """The folders that hold plugins, in order: the shipped ones, then the user's beside the queue."""
    if DIRS is not None:
        return list(DIRS)
    return [os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins"),
            os.path.join(os.path.dirname(os.path.abspath(core.db_path())), "plugins")]


def _bad(folder, why):
    return core.RiverError(f"the plugin in {folder} is not usable: {why}")


def _check(m, folder):
    """The manifest m of the plugin in folder, after a check of each field; refuses with what is wrong."""
    if not isinstance(m, dict):
        raise _bad(folder, f"{MANIFEST} is not a JSON object")
    if m.get("contract") != CONTRACT:
        raise _bad(folder, f"{MANIFEST} follows contract {m.get('contract')!r}; this MaximizePM reads contract "
                           f"{CONTRACT} (\"contract\": {CONTRACT})")
    name = m.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise _bad(folder, "its name is lower case letters, digits and '-', at most 40")
    if name != os.path.basename(folder.rstrip(os.sep)):
        raise _bad(folder, f"its name {name!r} is not the name of its folder")
    if name in RESERVED:
        raise _bad(folder, f"the name {name!r} is MaximizePM's own; choose another")
    if not isinstance(m.get("version"), str) or not m["version"].strip():
        raise _bad(folder, "it names no version (\"version\": \"1.0.0\")")
    if m.get("level") not in LEVELS:
        raise _bad(folder, f"its level is one of {', '.join(LEVELS)}")
    acts = m.get("actions", [])
    if not isinstance(acts, list) or any(a not in ACTIONS for a in acts) or len(set(acts)) != len(acts):
        raise _bad(folder, f"its actions are a list of {', '.join(ACTIONS)}")
    if m["level"] == "report" and acts:
        raise _bad(folder, "a report plugin lists no actions; its level is act when it does them")
    if m["level"] != "report" and not acts:
        raise _bad(folder, f"an {m['level']} plugin lists the actions it does")
    run = m.get("run")
    if not isinstance(run, list) or not run or not all(isinstance(w, str) and w for w in run):
        raise _bad(folder, "\"run\" is the command as a list of words, such as [\"{python}\", \"plugin.py\"]")
    for key in ("settings", "secrets"):
        v = m.get(key, {})
        if not isinstance(v, dict) or not all(isinstance(k, str) and re.match(r"^[a-z][a-z0-9_]{0,39}$", k)
                                              and isinstance(d, dict) for k, d in v.items()):
            raise _bad(folder, f"\"{key}\" maps each name (lower case, digits, '_') to an object with its help")
    for key, lo, hi in (("poll_every", POLL_MIN, 86400), ("timeout", 1, TIMEOUT_MAX)):
        v = m.get(key)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi):
            raise _bad(folder, f"\"{key}\" is a number of seconds from {lo} to {hi}")
    out = dict(m)
    out.update(actions=list(acts), settings=m.get("settings", {}), secrets=m.get("secrets", {}),
               poll_every=m.get("poll_every") or POLL_EVERY, timeout=m.get("timeout") or TIMEOUT,
               description=str(m.get("description") or ""), folder=folder)
    return out


def _read(folder):
    """The checked manifest of the plugin in folder, or the RiverError that says what is wrong (cached by mtime)."""
    path = os.path.join(folder, MANIFEST)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return core.RiverError(f"the plugin in {folder} has no {MANIFEST}")
    hit = _CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            got = _check(json.load(f), folder)
    except ValueError as e:
        got = _bad(folder, f"{MANIFEST} is not JSON ({e})")
    except core.RiverError as e:
        got = e
    _CACHE[path] = (mtime, got)
    return got


def find(name, need=True):
    """The manifest of the installed plugin name (the first folder in dirs() that has it), or None (need: refuse)."""
    if name and _NAME.match(name) and name not in RESERVED:
        for d in dirs():
            folder = os.path.join(d, name)
            if os.path.isdir(folder):
                got = _read(folder)
                if isinstance(got, core.RiverError):
                    if need:
                        raise got
                    return None
                return got
    if need:
        raise core.RiverError(f"no plugin {name!r} is installed (looked in {', '.join(dirs())}); maxpm pipeline "
                              "plugins lists the plugins")
    return None


def is_plugin(name):
    """Whether a pipeline's source (pipelines.forge) is a plugin that is installed and usable."""
    return bool(name) and name not in RESERVED and find(name, need=False) is not None


def listing():
    """maxpm pipeline plugins: each plugin folder, with its manifest or what is wrong with it."""
    out, seen = [], set()
    for d in dirs():
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            folder = os.path.join(d, name)
            if not os.path.isdir(folder) or name.startswith((".", "_")):
                continue
            got = _read(folder)
            row = {"name": name, "folder": folder, "shipped": d == dirs()[0] and DIRS is None}
            if isinstance(got, core.RiverError):
                row["error"] = str(got)
            else:
                row.update({k: got[k] for k in ("version", "level", "actions", "description")})
                row["settings"], row["secrets"] = sorted(got["settings"]), sorted(got["secrets"])
            if name in seen:
                row["error"] = "a plugin of the same name comes first; this one is not used"
            seen.add(name)
            out.append(row)
    return out


# ---------------------------------------------------------------- one call

def _argv(m):
    return [sys.executable if w == "{python}" else w for w in m["run"]]


def _run(argv, folder, request, timeout):
    """Run the plugin once: (exit code, stdout bytes, stderr text). Refuses when it does not start, runs past
    timeout, or prints more than OUT_MAX; it is then stopped with every process it started."""
    if RUNNER is not None:
        return RUNNER(argv, folder, request, timeout)
    env = {k: os.environ[k] for k in KEEP_ENV if k in os.environ}
    env["MAXPM_PLUGIN_CONTRACT"] = str(CONTRACT)
    try:
        p = subprocess.Popen(argv, cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=env, start_new_session=True)
    except OSError as e:
        raise core.RiverError(f"the plugin did not start ({' '.join(argv)}): {e}") from None
    out, err, big = bytearray(), bytearray(), []

    def write():
        try:
            p.stdin.write(json.dumps(request).encode())
            p.stdin.close()
        except OSError:
            pass  # it ended or closed its stdin before it read the request: its answer says so

    def read(stream, buf, cap, keep_tail):
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            buf.extend(chunk)
            if keep_tail and len(buf) > ERR_KEEP * 4:
                del buf[:-ERR_KEEP * 4]
            elif not keep_tail and len(buf) > cap:
                big.append(True)
                core._stop_group(p)
                return

    threads = [threading.Thread(target=write, daemon=True),
               threading.Thread(target=read, args=(p.stdout, out, OUT_MAX, False), daemon=True),
               threading.Thread(target=read, args=(p.stderr, err, 0, True), daemon=True)]
    for t in threads:
        t.start()
    try:
        code = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        core._stop_group(p)
        raise core.RiverError(f"the plugin ran past its time limit of {timeout}s and was stopped") from None
    finally:
        for t in threads:
            t.join(timeout=2)  # a process it left behind may hold its output open: do not wait for it
        for s in (p.stdout, p.stderr):
            try:
                s.close()
            except OSError:
                pass
    if big:
        raise core.RiverError(f"the plugin printed more than {OUT_MAX // (1024 * 1024)} MB and was stopped")
    return code, bytes(out), err.decode("utf-8", "replace")


def call(m, request):
    """One call of plugin m: its answer (a dict). Refuses with the plugin's own error, its stderr's last lines, or
    what is wrong with its answer."""
    request = dict(request, contract=CONTRACT)
    started = time.monotonic()
    code, out, err = _run(_argv(m), m["folder"], request, m["timeout"])
    tail = err.strip()[-ERR_KEEP:]
    answer = None
    try:
        answer = json.loads(out.decode("utf-8")) if out.strip() else None
    except (ValueError, UnicodeDecodeError):
        if code == 0:
            raise core.RiverError(f"plugin {m['name']}: its answer to {request['call']} is not one JSON object"
                                  + (f"; stderr: {tail}" if tail else "")) from None
    if isinstance(answer, dict) and answer.get("error"):
        raise core.RiverError(f"plugin {m['name']}: {str(answer['error'])[:ERR_KEEP]}")
    if code != 0:
        raise core.RiverError(f"plugin {m['name']} ended with exit {code} on {request['call']}"
                              + (f": {tail}" if tail else ""))
    if not isinstance(answer, dict):
        raise core.RiverError(f"plugin {m['name']}: its answer to {request['call']} is not one JSON object")
    answer["_seconds"] = round(time.monotonic() - started, 2)
    return answer


def describe(name):
    """maxpm pipeline plugin <name>: the manifest, and the plugin's own describe, which must match it."""
    m = find(name)
    got = call(m, {"call": "describe"})
    diff = [k for k in ("name", "version", "level") if got.get(k) != m[k]]
    if sorted(got.get("actions") or []) != sorted(m["actions"]):
        diff.append("actions")
    out = {k: m[k] for k in ("name", "version", "level", "actions", "description", "settings", "secrets",
                             "poll_every", "timeout", "folder")}
    out["describe"] = {k: v for k, v in got.items() if not k.startswith("_")}
    out["seconds"] = got["_seconds"]
    if diff:
        raise core.RiverError(f"plugin {name}: its describe differs from its {MANIFEST} in {', '.join(diff)}; "
                              "the manifest and the program must say the same")
    return out


# ---------------------------------------------------------------- a pipeline that uses a plugin

def _config(p):
    try:
        c = json.loads(p["plugin_settings"] or "{}")
    except ValueError:
        c = {}
    return {"settings": c.get("settings") or {}, "secrets": c.get("secrets") or {}}


def use(conn, pipeline, name, settings=None, secrets=None, repo=None, url=None, ref=None, actor=None):
    """maxpm pipeline forge <p> <plugin> [--set key=value] [--secret [key=]name]: the pipeline's runs come from the
    plugin. Each setting must be one the manifest names, and each required one set (--repo, --url and --ref fill the
    settings repo, url and ref); each secret the manifest names maps to a maxpm secret. The plugin's describe runs
    once, so a broken plugin is refused here and not at its first poll."""
    from . import define
    define._may(conn, actor)  # before the plugin runs: only a person or the manager connects a source
    store._pipeline(conn, pipeline)
    m = find(name)
    settings = dict(settings or {})
    for key, val in (("repo", repo), ("url", url), ("ref", ref)):
        if val is not None:
            settings[key] = val
    unknown = sorted(set(settings) - set(m["settings"]))
    if unknown:
        raise core.RiverError(f"plugin {name} has no setting {', '.join(unknown)}; its settings: "
                              f"{', '.join(sorted(m['settings'])) or 'none'}")
    for key, d in m["settings"].items():
        if key not in settings and "default" in d:
            settings[key] = d["default"]
    missing = [k for k, d in m["settings"].items() if d.get("required") and settings.get(k) in (None, "")]
    if missing:
        raise core.RiverError(f"plugin {name} needs " + ", ".join(
            f"--set {k}=<{m['settings'][k].get('help') or 'value'}>" for k in missing))
    mapped = {}
    for s in secrets or []:
        key, _, sec = s.rpartition("=")
        if not key:
            if len(m["secrets"]) != 1:
                raise core.RiverError(f"plugin {name} needs the secrets {', '.join(sorted(m['secrets'])) or 'none'}: "
                                      "name each one, --secret <its name>=<maxpm secret>")
            key = next(iter(m["secrets"]))
        if key not in m["secrets"]:
            raise core.RiverError(f"plugin {name} needs no secret {key}; it needs "
                                  f"{', '.join(sorted(m['secrets'])) or 'none'}")
        from .. import vault
        vault.check_name(sec)
        mapped[key] = sec
    missing = [k for k, d in m["secrets"].items() if d.get("required", True) and k not in mapped]
    if missing:
        raise core.RiverError(f"plugin {name} needs " + ", ".join(
            f"--secret {k}=<maxpm secret: {m['secrets'][k].get('help') or 'its value'}>" for k in missing)
            + " (a person saves it: maxpm secret put <name>)")
    describe(name)
    cfg = json.dumps({"settings": settings, "secrets": mapped}, sort_keys=True)
    with core.tx(conn):
        define._may(conn, actor)
        p = store._pipeline(conn, pipeline)
        conn.execute("UPDATE pipelines SET forge=?, repo=?, secret=?, ref=?, forge_url=?, plugin_settings=?, "
                     "plugin_state=NULL, polled_at=NULL, poll_error=NULL WHERE id=?",
                     (name, settings.get("repo"), next(iter(mapped.values()), None), settings.get("ref"),
                      settings.get("url"), cfg, p["id"]))
        text = (f"source plugin {name} {m['version']} ({m['level']}"
                + (f": {', '.join(m['actions'])}" if m["actions"] else "") + ")"
                + "".join(f", {k}={v}" for k, v in sorted(settings.items()))
                + "".join(f", secret {k} from {v}" for k, v in sorted(mapped.items())))
        define._change(conn, p["id"], actor, text, {"plugin": name, "settings": settings, "secrets": mapped})
    return {"pipeline": p["name"], "forge": name, "plugin": True, "version": m["version"], "level": m["level"],
            "actions": m["actions"], "settings": settings, "secrets": mapped}


def _secret_values(conn, p, m):
    cfg, out = _config(p), {}
    for key in m["secrets"]:
        sec = cfg["secrets"].get(key)
        if not sec:
            continue
        got = _SECRETS.get(sec)
        if got and time.monotonic() - got[1] < TOKEN_KEEP:
            out[key] = got[0]
            continue
        from .. import vault
        value = vault.get(conn, sec, use=f"pipeline {p['name']}: plugin {m['name']}").decode().strip()
        _SECRETS[sec] = (value, time.monotonic())
        out[key] = value
    return out


def _pipeline_info(conn, p):
    envs = [{"name": e["name"], "target": e["target"], "version": e["rev"]} for e in conn.execute(
        "SELECT e.name, e.target, v.rev FROM pipeline_envs e LEFT JOIN pipeline_versions v ON v.id=e.version_id "
        "WHERE e.pipeline_id=? AND e.removed_at IS NULL ORDER BY e.id", (p["id"],))]
    stages = [{"name": s["name"], "kind": s["kind"], "pos": s["pos"], "work": s["workflow"], "env": s["env"],
               "transition": s["mode"] or "auto"} for s in conn.execute(
        "SELECT s.*, e.name env, t.mode FROM pipeline_stages s LEFT JOIN pipeline_envs e ON e.id=s.env_id "
        "LEFT JOIN pipeline_transitions t ON t.stage_id=s.id WHERE s.pipeline_id=? AND s.removed_at IS NULL "
        "ORDER BY s.pos, s.id", (p["id"],))]
    return {"name": p["name"], "project": p["project"], "stages": stages, "envs": envs}


def _run_info(r):
    return {"id": r["id"], "stage": r["stage"], "work": r["workflow"], "version": r["rev"], "key": r["key"],
            "state": r["state"], "purpose": r["purpose"], "url": r["url"]}


def _open_runs(conn, p):
    from .runner import _row
    return [_run_info(_row(conn, r["id"])) for r in conn.execute(
        "SELECT id FROM stage_runs WHERE pipeline_id=? AND state IN ('queued','running') ORDER BY id DESC LIMIT 100",
        (p["id"],))]


def _request(conn, p, m, what):
    return {"call": what, "pipeline": _pipeline_info(conn, p), "settings": _config(p)["settings"],
            "secrets": _secret_values(conn, p, m), "state": json.loads(p["plugin_state"] or "null")}


def _own(ev, name, pipeline):
    """An event of the plugin: its source is the plugin, and its id is kept once for each pipeline."""
    if not isinstance(ev, dict) or not isinstance(ev.get("context"), dict):
        return ev  # cdevents.parse refuses it with the reason
    ev = dict(ev)
    ctx = dict(ev["context"])
    ctx["source"] = name
    if ctx.get("id"):
        ctx["id"] = f"{pipeline}:{ctx['id']}"
    ev["context"] = ctx
    return ev


def _take(conn, p, m, answer):
    """Keep what an answer brings: its events (in one transaction, all or none) and its state. A run that the
    events end as passed hands its version on (runner.hand_on). Returns the number of new events."""
    from . import runner
    events = answer.get("events") or []
    if not isinstance(events, list):
        raise core.RiverError(f"plugin {m['name']}: \"events\" is a list of CDEvents objects")
    if len(events) > EVENTS_MAX:
        raise core.RiverError(f"plugin {m['name']}: more than {EVENTS_MAX} events in one answer; keep the rest for "
                              "the next poll (its state)")
    res = store.ingest(conn, [_own(e, m["name"], p["name"]) for e in events], pipeline=p["name"]) if events else []
    if "state" in answer:
        state = json.dumps(answer["state"], separators=(",", ":"))
        if len(state) > STATE_MAX:
            raise core.RiverError(f"plugin {m['name']}: its state is more than {STATE_MAX // 1024} KB")
        with core.tx(conn):
            conn.execute("UPDATE pipelines SET plugin_state=? WHERE id=?", (state, p["id"]))
    new = [r for r in res if not r.get("duplicate")]
    for r in new:
        if r["type"].startswith("dev.cdevents.pipelinerun.finished") and r["run"]:
            row = conn.execute("SELECT state FROM stage_runs WHERE id=?", (r["run"],)).fetchone()
            if row and row["state"] == "passed":
                runner.hand_on(conn, r["run"])
    return len(new)


def poll(conn, p):
    """Read one pipeline's system through its plugin: {'events': new events, 'seconds'}."""
    m = find(p["forge"])
    req = _request(conn, p, m, "poll")
    req["runs"] = _open_runs(conn, p)
    answer = call(m, req)
    return {"events": _take(conn, p, m, answer), "seconds": answer["_seconds"]}


def poll_due(conn):
    """One pass of maxpm serve: poll each pipeline whose source is a plugin and whose last poll is the plugin's
    poll_every old. An error stays on its pipeline (poll_error) and does not stop the others."""
    out = {}
    now = core.now()
    for p in conn.execute("SELECT * FROM pipelines WHERE forge IS NOT NULL AND forge NOT IN (%s)"
                          % ",".join("?" * len(RESERVED)), sorted(RESERVED)).fetchall():
        m = find(p["forge"], need=False)
        every = m["poll_every"] if m else POLL_EVERY
        if p["polled_at"] and core.parse_iso(p["polled_at"]) > now - core.timedelta(seconds=every):
            continue
        try:
            if m is None:
                find(p["forge"])  # the reason
            out[p["name"]], err = poll(conn, p), None
        except core.RiverError as e:
            out[p["name"]], err = {"error": str(e)}, str(e)[:500]
        except Exception as e:  # noqa: BLE001 - a plugin's bad answer must not stop the loop of maxpm serve
            out[p["name"]], err = {"error": f"{type(e).__name__}: {e}"}, f"{type(e).__name__}: {e}"[:500]
        with core.tx(conn):
            conn.execute("UPDATE pipelines SET polled_at=?, poll_error=? WHERE id=?",
                         (core.iso(core.now()), err, p["id"]))
    return out


# ---------------------------------------------------------------- actions

def action_for(purpose):
    """The plugin call that starts a stage run of this purpose (stage_runs.purpose)."""
    if purpose == "rollback":
        return "rollback"
    if purpose == "rerun" or (purpose or "").startswith("failed:"):
        return "rerun"
    return "start"


def actions(conn, p):
    """What the source of pipeline p can do, for the page and the refusals: {'kind', 'name', 'actions', ...}.
    kind here: the stages' commands run on this computer (every action); adapter: a built-in forge adapter;
    plugin: an installed plugin (its level, version and actions); missing: a plugin that is not installed."""
    src = p["forge"]
    if not src:
        return {"kind": "here", "name": None, "actions": ALL_ACTIONS}
    if src in BUILTIN_ACTIONS:
        return {"kind": "adapter", "name": src, "actions": BUILTIN_ACTIONS[src]}
    try:
        m = find(src)
    except core.RiverError as e:
        return {"kind": "missing", "name": src, "actions": [], "error": str(e)}
    return {"kind": "plugin", "name": src, "version": m["version"], "level": m["level"], "actions": m["actions"]}


def need(conn, p, stage, action):
    """Refuse an action on a stage whose work the pipeline's source does (a workflow and no command) when the source
    does not list it: 'not available for this source'."""
    if not stage["workflow"] or stage["run"]:
        return
    src = actions(conn, p)
    if action not in src["actions"]:
        if src["kind"] == "adapter":
            from .runner import FORGES
            raise core.RiverError(f"{action} is not available for this source: MaximizePM cannot do it through the "
                                  f"{FORGES[src['name']]} API; do it on {FORGES[src['name']]}'s own page")
        raise core.RiverError(f"{action} is not available for this source: " + (
            src.get("error") or f"plugin {src['name']} lists only {', '.join(src['actions']) or 'no actions'}")
            + f"; do it in that system, or use a plugin that lists {action}")


def act(conn, run_id, action):
    """Ask the plugin of a stage run's pipeline to do an action on the run (start, rerun, rollback, stop). Its
    answer may carry 'key' (the outside system's id of the run, so later events find it), 'url', 'note' and
    events. A plugin that cannot start runs (level report) leaves a started stage to its system."""
    from .runner import _row
    r = _row(conn, run_id)
    p = conn.execute("SELECT * FROM pipelines WHERE id=?", (r["pipeline_id"],)).fetchone()
    m = find(p["forge"])
    if action not in m["actions"]:
        if action == "start":
            with core.tx(conn):
                conn.execute("UPDATE stage_runs SET runner=?, gate_note=? WHERE id=?",
                             (f"forge:{m['name']}", f"its gate opened; {m['name']} starts it in its own system "
                              "(this plugin starts no run)", run_id))
            return {"run": run_id, "plugin": m["name"], "left": True}
        raise core.RiverError(f"{action} is not available for this source: plugin {m['name']} lists only "
                              f"{', '.join(m['actions']) or 'no actions'}")
    req = _request(conn, p, m, action)
    req["run"] = _run_info(r)
    if (r["purpose"] or "").startswith("failed:"):
        req["tests"] = [f"{t['suite']}.{t['name']}" for t in store.tests(conn, int(r["purpose"][7:]))
                        if t["state"] in ("failed", "error")]
    answer = call(m, req)
    key, url, note = answer.get("key"), answer.get("url"), answer.get("note")
    with core.tx(conn):
        if action != "stop":
            if key is not None and not conn.execute("SELECT 1 FROM stage_runs WHERE pipeline_id=? AND key=? AND id<>?",
                                                    (r["pipeline_id"], str(key), run_id)).fetchone():
                conn.execute("UPDATE stage_runs SET key=?, source=? WHERE id=?", (str(key), m["name"], run_id))
            conn.execute("UPDATE stage_runs SET runner=?, url=COALESCE(?, url), gate_note=? WHERE id=?",
                         (f"forge:{m['name']}", url, str(note or f"started by {m['name']}; its run shows at its "
                                                                 "next poll")[:500], run_id))
    _take(conn, p, m, answer)
    return {"run": run_id, "plugin": m["name"], action: True, "note": note}


def plugin_run(conn, r):
    """Whether stage run r (runner._row) is in the hands of a plugin: started by it, or made by its events."""
    src = r["forge"]
    if not src or src in RESERVED or not (r["workflow"] and not r["cmd"]):
        return False
    return (r["runner"] or "") == f"forge:{src}" or r["source"] == src
