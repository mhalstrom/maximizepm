"""#6208: an artifact (a canvas, a file, a page, a claude.ai artifact) links back to the tasks and questions that
review or name it, and a task links to its artifacts."""
import contextlib
import io
import os

from river import core
from river.core import RiverError
from tests.test_core import Base


class ArtifactTaskLinks(Base):
    def setUp(self):
        super().setUp()
        core.project_add(self.c, "web")
        core.project_add(self.c, "api")
        core.register(self.c, "ag")
        core.register(self.c, "mark", human=True)
        self.made = self.add("web", "Write the README section")
        core.claim(self.c, self.made, "ag")
        self.cv = core.canvas_put(self.c, "planner-readme", "# Plan\n", "ag", kind="md", item=self.made)["id"]
        self.file = os.path.join(self.dir.name, "planner-readme.md")
        with open(self.file, "w") as f:
            f.write("# Plan\n")

    def run_cli(self, *words):
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.run(["--as", "ag", *words])
        return out.getvalue()

    def test_a_task_that_names_a_canvas_links_both_ways(self):
        look = core.item_add(self.c, "web", "Look at canvas planner-readme: commit it after mark's yes", doer="human",
                             actor="ag", context=f"the file {self.file}")["id"]
        tasks = core.canvas_show(self.c, "planner-readme")["tasks"]
        self.assertEqual([(t["item"], t["how"]) for t in tasks], [(self.made, "made for"), (look, "named")])
        self.assertEqual(tasks[1]["state"], "open")
        links = core.item_show(self.c, look)["artifact_links"]
        self.assertEqual([(x["kind"], x["target"]) for x in links],
                         [("canvas", f"canvas:{self.cv}"), ("file", self.file)])
        self.assertEqual(links[0]["canvas"], {"name": "planner-readme", "project": "web", "version": 1})
        self.assertEqual(core.artifact_tasks(self.c, [self.file])[self.file][0]["item"], look)
        # The item that made the canvas lists it too; its own link is not repeated when its text names it.
        self.assertEqual([x["how"] for x in core.item_show(self.c, self.made)["artifact_links"]], ["made for"])
        core.item_edit(self.c, self.made, context="see canvas planner-readme", actor="ag")
        self.assertEqual([t["item"] for t in core.artifact_tasks(self.c, [f"canvas:{self.cv}"])[f"canvas:{self.cv}"]],
                         [self.made, look])
        # Done: the state follows.
        core.done(self.c, look, "committed", "mark")
        self.assertEqual(core.canvas_show(self.c, "planner-readme")["tasks"][1]["state"], "done")

    def test_a_question_to_a_person_links_its_artifacts(self):
        q = core.send(self.c, "question", f"Approve the section (canvas planner-readme v2, file {self.file})? "
                      "Look at https://claude.ai/artifact/AbC-12/x too", to="mark", item=self.made, actor="ag")["id"]
        art = "https://claude.ai/artifact/AbC-12"
        tasks = core.artifact_tasks(self.c, [f"canvas:{self.cv}", art])
        mine = [t for t in tasks[f"canvas:{self.cv}"] if t["message"]]
        self.assertEqual([(t["message"], t["item"], t["state"], t["to"]) for t in mine],
                         [(q, self.made, "waits for an answer", "mark")])
        self.assertEqual(tasks[art][0]["message_kind"], "question")
        links = core.message_artifacts(self.c, [q])[q]
        self.assertEqual([x["kind"] for x in links], ["canvas", "file", "artifact"])
        core.answer(self.c, q, "yes", "mark")
        mine = [t for t in core.artifact_tasks(self.c, [art])[art] if t["message"]]
        self.assertEqual(mine[0]["state"], "answered")
        # A note links nothing by its text; --artifact links it.
        n = core.send(self.c, "note", "canvas planner-readme", to="mark", actor="ag")["id"]
        self.assertEqual(core.message_artifacts(self.c, [n])[n], [])
        n = core.send(self.c, "note", "see it", to="mark", actor="ag", artifacts=["planner-readme"])["id"]
        self.assertEqual([x["target"] for x in core.message_artifacts(self.c, [n])[n]], [f"canvas:{self.cv}"])

    def test_the_artifact_flag_and_what_it_refuses(self):
        it = core.item_add(self.c, "web", "Review the draft", actor="ag",
                           artifacts=[f"canvas:{self.cv}", self.file, "http://localhost:8080/page"])["id"]
        self.assertEqual([(x["kind"], x["how"]) for x in core.item_show(self.c, it)["artifact_links"]],
                         [("canvas", "flag"), ("file", "flag"), ("page", "flag")])
        with self.assertRaisesRegex(RiverError, "no canvas of that name"):
            core.item_add(self.c, "web", "Review", actor="ag", artifacts=["no-such-canvas"])
        with self.assertRaisesRegex(RiverError, "no such file"):
            core.item_edit(self.c, it, actor="ag", artifacts=["/no/such/file.md"])
        # Command words after 'canvas' and a canvas of no project known by that name link nothing.
        core.canvas_put(self.c, "show", "x", "ag", kind="md", project="web")
        other = core.item_add(self.c, "web", "Run maxpm canvas show and canvas put for canvas nothing", actor="ag")["id"]
        self.assertEqual(core.item_show(self.c, other)["artifact_links"], [])
        # A name in two projects: the item's own project wins.
        core.canvas_put(self.c, "planner-readme", "y", "ag", kind="md", project="api")
        api = core.item_add(self.c, "api", "Look at canvas planner-readme", actor="ag")["id"]
        self.assertEqual(core.item_show(self.c, api)["artifact_links"][0]["canvas"]["project"], "api")

    def test_cli_and_artifacts_list_show_the_links(self):
        look = core.item_add(self.c, "web", "Look at canvas planner-readme", actor="ag", short="Look at the plan")["id"]
        text = self.run_cli("canvas", "show", "planner-readme")
        self.assertIn(f"made for #{self.made}", text)
        self.assertIn(f"reviewed in #{look} Look at the plan (open)", text)
        self.assertIn(f"reviews or names:\n    canvas planner-readme v1 (web): maxpm canvas show planner-readme "
                      f"--project web", self.run_cli("show", str(look)))
        from river import server  # noqa: F401  (loads the routes)
        from river.routes import artifacts as art
        rows = art.listing(self.c, item=self.made)
        canvas = next(r for r in rows if r["kind"] == "canvas")
        self.assertEqual([t["item"] for t in canvas["tasks"]], [self.made, look])
        self.assertIn("reviews or names: canvas planner-readme", self.run_cli("show", str(look), "--brief"))
        # The go briefing of the reviewing task names the canvas.
        from river import cli
        folder = os.path.join(self.dir.name, "web")
        os.makedirs(folder)
        core.project_path(self.c, "web", folder)
        b = core.go(self.c, folder, "w2")
        self.assertEqual(b["item"]["id"], look)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(b)
        self.assertIn("reviews or names (read it first):\n    canvas planner-readme v1 (web)", out.getvalue())

    def test_the_one_time_fill_links_older_items_and_questions(self):
        cur = self.c.execute("INSERT INTO items(project_id,title,notes,priority,rank,doer,context,touches,\"check\","
                             "created_at) SELECT id, 'Approve canvas planner-readme', '', 2, 99, 'human', '', '', '', ? "
                             "FROM projects WHERE name='web'", (core.iso(core.now()),))
        old = cur.lastrowid
        self.c.execute("DELETE FROM artifact_links")
        self.c.execute("DELETE FROM meta WHERE key='artifact_links'")
        self.c.commit()
        core._migrate_artifact_links(self.c)
        self.assertEqual([t["item"] for t in core.canvas_show(self.c, "planner-readme")["tasks"]], [self.made, old])
        core._migrate_artifact_links(self.c)  # once only
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM artifact_links").fetchone()[0], 1)
