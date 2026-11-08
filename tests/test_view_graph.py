"""The Graph tab of the page (#4656): a large queue draws."""
import contextlib
import io
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from river import core, server
from river.core import RiverError
from tests.test_server import _chrome


def big_queue(n, open_items, projects=17, seed=696):
    """The items of a queue as the page gets them (the fields the graph reads): the first ones finished,
    the last open_items open, each waiting on up to two earlier items."""
    rnd = random.Random(seed)
    items = []
    for k in range(1, n + 1):
        waits = sorted(rnd.sample(range(max(1, k - 60), k), min(k - 1, rnd.choice((0, 0, 1, 1, 1, 2, 2)))))
        items.append({"id": k, "title": f"Check the import of orders for the reports page, part {k}",
                      "project": f"project-{rnd.randrange(projects):02d}", "doer": "human" if k % 23 == 0 else "any",
                      "status": "done" if k <= n - open_items else "open", "waits_on": waits, "unblocks": []})
    for i in items:
        for w in i["waits_on"]:
            items[w - 1]["unblocks"].append(i["id"])
        i["ready"] = i["status"] == "open" and all(items[w - 1]["status"] == "done" for w in i["waits_on"])
    return {"projects": [{"id": n, "name": f"project-{n:02d}"} for n in range(projects)], "items": items}


@unittest.skipUnless(shutil.which("node"), "node is not on this machine")
class GraphOfALargeQueue(unittest.TestCase):
    """The Graph tab draws a queue of 700 items, most of them finished (#696). Mermaid's own limits are 50,000
    characters and 500 edges; above them it draws an error picture. Node runs the page's own component
    (components/graphText.js) here; the drawing itself needs a browser (seed/large.py loads a queue for that)."""

    STATIC = Path(server.__file__).parent / "static"

    def graph(self, queue, project=None, done=True):
        code = (self.STATIC / "components" / "graphText.js").read_text(encoding="utf-8") + f"""
const S = {json.dumps(queue)}, proj = {json.dumps(project)}, fin = (i) => i.status === "done";
const items = S.items.filter(i => {json.dumps(done)} || !fin(i));
const {{ show, total, hidden }} = graphItems(items, proj ? items.filter(i => i.project === proj) : null);
const g = graphText(S.projects, items, show);
console.log(JSON.stringify({{ shown: [...show], total, hidden, chars: g.text.length, nodes: g.nodes, edges: g.edges,
  lines: g.text.split("\\n").length, limits: GRAPH_LIMITS, max: GRAPH_MAX }}));
"""
        r = subprocess.run(["node", "--input-type=module"], input=code, capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_the_page_reads_fields_that_the_state_has(self):
        with tempfile.TemporaryDirectory() as d:
            c = core.connect(os.path.join(d, "t.db"))
            core.project_add(c, "a")
            core.item_add(c, "a", "one")
            st = core.state(c)
            c.close()
        self.assertLessEqual(set(big_queue(1, 1)["items"][0]), set(st["items"][0]))
        self.assertLessEqual({"id", "name"}, set(st["projects"][0]))

    def test_700_items_with_the_finished_ones_are_all_drawn_inside_the_limits(self):
        q = big_queue(700, 70)
        links = sum(len(i["waits_on"]) for i in q["items"])
        g = self.graph(q)
        self.assertEqual((g["nodes"], g["edges"], g["hidden"], g["total"]), (700, links, 0, 700))
        # More than Mermaid allows by default, so the page must raise both limits...
        self.assertGreater(g["chars"], 50000)
        self.assertGreater(g["edges"], 500)
        # ...and it does, with room: mermaid.initialize gets the limits of the component.
        self.assertLess(g["chars"] * 5, g["limits"]["maxTextSize"])
        self.assertLess(g["edges"] * 5, g["limits"]["maxEdges"])
        self.assertRegex((self.STATIC / "app.js").read_text(encoding="utf-8"), r"mermaid\.initialize\(\{[^;]*\.\.\.GRAPH_LIMITS \}\)")
        # Without the finished items: the open ones only.
        g = self.graph(q, done=False)
        self.assertEqual((g["nodes"], g["hidden"]), (70, 0))

    def test_above_the_item_limit_finished_items_away_from_open_work_are_left_out(self):
        q = big_queue(2000, 120)
        g = self.graph(q)
        by = {i["id"]: i for i in q["items"]}
        is_open = lambda x: by[x]["status"] == "open"
        self.assertGreater(2000, g["max"])
        self.assertEqual(g["total"], 2000)
        self.assertEqual(g["hidden"], 2000 - len(g["shown"]))
        self.assertGreater(g["hidden"], 1000)
        self.assertLessEqual({i for i in by if is_open(i)}, set(g["shown"]))  # every open item
        for x in g["shown"]:  # and of the finished ones, those next to an open item
            self.assertTrue(is_open(x) or any(is_open(y) for y in by[x]["waits_on"] + by[x]["unblocks"]), x)
        self.assertEqual(g["nodes"], len(g["shown"]))
        # At the limit itself nothing is left out.
        g = self.graph(big_queue(g["max"], 100))
        self.assertEqual((g["hidden"], g["nodes"]), (0, g["max"]))

    def test_a_project_shows_its_items_and_their_neighbors(self):
        q = big_queue(700, 70)
        mine = [i for i in q["items"] if i["project"] == "project-03"]
        want = {x for i in mine for x in [i["id"], *i["waits_on"], *i["unblocks"]]}
        g = self.graph(q, project="project-03")
        self.assertEqual((set(g["shown"]), g["hidden"]), (want, 0))
