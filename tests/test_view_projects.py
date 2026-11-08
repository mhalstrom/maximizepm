"""The Projects tab of the page (#4656): goals on the page."""
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


class PageGoals(unittest.TestCase):
    """Goals on the page: the state carries goals with progress; the goal ops and item tags work."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()
        core.project_add(self.c, "a")
        core.register(self.c, "ag")

    def tearDown(self):
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def op(self, _op, who, **args):
        return server.OPS[_op](self.c, args, who)

    def get(self, path):
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.headers, out = path, ("127.0.0.1", 5555), {"Host": "127.0.0.1:8765"}, {}
        h._send = lambda code, body, ctype=None: out.update(code=code, body=body)
        h.do_GET()
        return out["code"], out["body"]

    def test_item_models_through_the_page(self):
        i = self.op("item_add", None, project="a", title="x", models={"model": "opus", "effort": "high"})["id"]
        self.op("item_edit", None, id=i, models={"max_model": "fable", "effort": "none"})
        code, st = self.get("/api/state")
        it = next(x for x in st["items"] if x["id"] == i)
        self.assertEqual((it["model"], it["effort"], it["max_model"]), ("opus", None, "fable"))
        self.assertEqual(st["effort_levels"], ["low", "medium", "high", "xhigh", "max"])
        self.assertIn("claude", st["model_ladder"])

    def test_goal_ops_tags_and_state(self):
        self.op("goal_add", None, project="a", name="ship", outcome="it ships", done_when="users can install it")
        self.op("goal_add", None, project="a", name="docs")
        one = self.op("item_add", None, project="a", title="build", goals=["ship"])["id"]
        two = self.op("item_add", None, project="a", title="loose")["id"]
        self.op("item_edit", None, id=two, goals=["docs"])
        self.op("item_edit", None, id=two, untag=["docs"])
        self.op("goal_rank", None, name="docs", rank=1)
        self.op("goal_own", "ag", name="ship")
        code, st = self.get("/api/state")
        goals = {g["name"]: g for g in st["goals"]}
        self.assertEqual([g["name"] for g in st["goals"]], ["docs", "ship"])
        self.assertEqual((goals["ship"]["owner"], goals["ship"]["items_open"]), ("ag", [one]))
        self.assertEqual({i["id"]: i["goals"] for i in st["items"]}, {one: ["ship"], two: []})
        self.op("goal_done", "ag", name="ship", result="shipped", drop_open=True)
        self.op("goal_edit", None, name="docs", outcome="readers find answers", done_when="guide exists")
        code, st = self.get("/api/state")
        goals = {g["name"]: g for g in st["goals"]}
        self.assertEqual((goals["ship"]["status"], goals["ship"]["result"]), ("complete", "shipped"))
        self.assertEqual(goals["docs"]["outcome"], "readers find answers")
        self.op("goal_reopen", None, name="ship")
        self.assertEqual(self.op("goal_edit", None, name="ship", shared=True)["shared"], 1)  # the page: no owner
        with self.assertRaisesRegex(Exception, "shared"):
            self.op("goal_own", "ag", name="ship")
        self.assertEqual(core.goal_show(self.c, "ship")["status"], "open")
