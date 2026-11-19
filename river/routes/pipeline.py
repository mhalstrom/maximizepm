"""The pipeline view of Plan (#6116): the reads that river/static/views/pipeline.js draws. The actions (hold, approve,
roll back, run again) come with #6117.

GET /api/pipelines            the pipelines, and each deploy target that no pipeline covers (its default pipeline)
GET /api/pipeline/<name>      one pipeline: its stages with their last run, its environments, and its versions
GET /api/pipeline-run/<id>    one stage run: its steps, its tests (failed first), its last lines, and the prompt that
                              analyzes its failed tests"""

from urllib.parse import unquote

from .. import core
from ..core import RiverError
from ..pipeline import prompt, store, targets

TESTS_MAX = 2000  # the test results one read sends


def _with(fn):
    def handler(h, path):
        conn = core.connect()
        try:
            return h._send(200, fn(conn, path))
        except (RiverError, ValueError) as e:
            return h._send(404, {"error": str(e)})
        finally:
            conn.close()
    return handler


def pipelines(conn, path):
    out = []
    for p in store.pipelines(conn):
        envs = [r["target"] for r in conn.execute("SELECT target FROM pipeline_envs WHERE pipeline_id=? AND target IS "
                                                  "NOT NULL AND removed_at IS NULL", (p["id"],))]
        out.append({"name": p["name"], "project": p["project"], "default": False, "targets": envs})
    for t in targets.default_list(conn):
        projects = [r["name"] for r in conn.execute("SELECT name FROM projects WHERE target=? AND archived=0 AND name<>? "
                                                    "ORDER BY rank, id", (t["name"], f"deploy-{t['name']}"))]
        out.append({"name": t["name"], "project": projects[0] if projects else None, "projects": projects,
                    "default": True, "targets": [t["name"]]})
    return {"pipelines": out}


def pipeline(conn, path):
    name = unquote(path.split("/api/pipeline/", 1)[1])
    if conn.execute("SELECT 1 FROM pipelines WHERE name=?", (name,)).fetchone():
        p = store.show(conn, name)
        p["versions"] = store.versions(conn, name)
        return p
    p = targets.default_pipeline(conn, name)
    p["versions"] = [{"rev": r["version"], "deploy": r["deploy"], "stages": r["stages"]} for r in p.pop("releases")]
    return p


def run(conn, path):
    run_id = int(path.rsplit("/", 1)[1])
    r = store.run_show(conn, run_id)
    r["test_list"] = store.tests(conn, run_id, limit=TESTS_MAX)
    if r["tests"].get("failed") or r["tests"].get("error"):
        r["prompt"] = prompt.failure_prompt(conn, run_id)["text"]
    return r


GET = [
    (lambda path: path == "/api/pipelines", _with(pipelines)),
    (lambda path: path.startswith("/api/pipeline-run/"), _with(run)),
    (lambda path: path.startswith("/api/pipeline/"), _with(pipeline)),
]

def _runner():
    from ..pipeline import runner
    return runner


# The page's pipeline actions (#6117), as the person chosen in "You are": each one is the maxpm pipeline command of the
# same name, with its checks (only a person or the manager acts) and a line in the pipeline's history.
OPS = {
    "pipeline_hold": lambda c, a, who: _runner().hold(c, a["pipeline"], a.get("stage"), a.get("reason"), who),
    "pipeline_unhold": lambda c, a, who: _runner().release_hold(c, a["pipeline"], a.get("stage"), who),
    "pipeline_approve": lambda c, a, who: _runner().approve(c, a["pipeline"], a["stage"], a.get("rev"), who),
    "pipeline_rerun": lambda c, a, who: _runner().run_again(c, a["pipeline"], a["stage"], a["rev"], who),
    "pipeline_rerun_failed": lambda c, a, who: _runner().rerun_failed(c, int(a["run"]), who),
    "pipeline_rollback": lambda c, a, who: _runner().rollback(c, a["pipeline"], a["env"], a["rev"], a.get("reason"), who),
    "pipeline_stop": lambda c, a, who: _runner().stop(c, int(a["run"]), who),
}
