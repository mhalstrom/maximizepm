"""The page's state as versions and changes (#4709): /api/state?since=<v> answers only what changed."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from river import core, server, statedelta


def apply(old, ans):
    """The same as static/state.js applyAnswer, for the tests that run without node."""
    if "state" in ans or old is None:
        return ans["state"]
    new = dict(old, **ans.get("parts", {}))
    for name in ans.get("drop", []):
        new.pop(name, None)
    for name, ch in ans.get("rows", {}).items():
        key = ch["key"]
        by = {r[key]: r for r in old.get(name, [])}
        for k in ch.get("del", []):
            by.pop(k, None)
        for r in ch.get("set", []):
            by[r[key]] = r
        if "order" in ch:
            new[name] = [by[k] for k in ch["order"]]
        else:
            gone = set(ch.get("del", []))
            new[name] = [by[r[key]] for r in old.get(name, []) if r[key] not in gone]
    return new


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.c = core.connect()
        statedelta.reset()
        core.project_add(self.c, "a")
        core.register(self.c, "ag")
        self.one = core.item_add(self.c, "a", "one", context="a long context " * 50)["id"]
        self.two = core.item_add(self.c, "a", "two", after=[self.one])["id"]
        self.old = core.item_add(self.c, "a", "old")["id"]
        core.claim(self.c, self.old, "ag")
        core.done(self.c, self.old, actor="ag", output="y" * 1000)

    def tearDown(self):
        self.c.close()
        os.environ.pop("MAXPM_DB", None)
        self.dir.cleanup()

    def get(self, path):
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.headers, out = path, ("127.0.0.1", 5555), {"Host": "127.0.0.1:8765"}, {}
        h._send = lambda code, body, ctype=None: out.update(code=code, body=json.loads(json.dumps(body, default=str)))
        h.do_GET()
        self.assertEqual(out["code"], 200, out)
        return out["body"]


class PageState(Base):
    def test_a_page_that_starts_from_nothing_gets_the_whole_state_without_done_items(self):
        ans = self.get("/api/state?since=")
        st = ans["state"]
        self.assertTrue(ans["v"])
        self.assertEqual([i["id"] for i in st["items"]], [self.one, self.two])
        self.assertEqual(st["closed_counts"], {"a": 1})
        self.assertNotIn("context", st["items"][0], "the drawer loads the item itself")
        for key in ("agents", "projects", "goals", "relay", "sessions", "page_build"):
            self.assertIn(key, st)
        # An unknown version (a restarted server, a page away for long) also gets the whole state.
        self.assertIn("state", self.get("/api/state?since=gone-1"))

    def test_without_since_the_old_whole_state(self):
        st = self.get("/api/state")
        self.assertNotIn("v", st)
        self.assertEqual({i["id"] for i in st["items"]}, {self.one, self.two, self.old})
        self.assertIn("context", st["items"][0])

    def test_the_changes_rebuild_the_state(self):
        first = self.get("/api/state?since=")
        page = first["state"]
        same = self.get(f"/api/state?since={first['v']}")
        self.assertEqual(same["rows"], {}, "nothing changed in the rows")
        self.assertLess(len(json.dumps(same)), 3000)
        self.assertLessEqual(set(same["parts"]), {"now", "loop", "relay", "capacity", "events"})
        page = apply(page, same)
        # Changes of every kind: a claim, a new item, an item done, a new agent, an item dropped.
        core.claim(self.c, self.one, "ag")
        three = core.item_add(self.c, "a", "three", priority=0)["id"]
        core.register(self.c, "other")
        core.drop(self.c, self.two, actor="ag")
        ans = self.get(f"/api/state?since={same['v']}")
        self.assertNotIn("state", ans)
        self.assertEqual({r["id"] for r in ans["rows"]["items"]["set"]}, {self.one, three})
        self.assertEqual(ans["rows"]["items"]["del"], [self.two])
        self.assertIn("order", ans["rows"]["items"])
        self.assertEqual([r["name"] for r in ans["rows"]["agents"]["set"]], ["ag", "other"], "ag holds #one now")
        page = apply(page, ans)
        whole = self.get("/api/state?since=")["state"]
        for part in ("now", "loop"):
            page.pop(part), whole.pop(part)
        self.assertEqual(page, whole)

    def test_done_items_come_and_go_with_closed(self):
        first = self.get("/api/state?since=")
        ans = self.get(f"/api/state?since={first['v']}&closed=1")
        self.assertEqual([r["id"] for r in ans["rows"]["items"]["set"]], [self.old])
        row = ans["rows"]["items"]["set"][0]
        self.assertEqual(len(row["output"]), statedelta.CLOSED_TEXT + 1, "long text of a done item is cut")
        self.assertNotIn("blocked_reason", row, "empty fields of a done item are left out")
        page = apply(first["state"], ans)
        self.assertEqual({i["id"] for i in page["items"]}, {self.one, self.two, self.old})
        back = self.get(f"/api/state?since={ans['v']}")
        self.assertEqual(back["rows"]["items"]["del"], [self.old])

    def test_a_large_queue_polls_small(self):
        for n in range(300):
            i = core.item_add(self.c, "a", f"item {n}", context="why and where " * 40)["id"]
            if n % 3:
                core.claim(self.c, i, "ag")
                core.done(self.c, i, actor="ag", output="what changed " * 40)
        whole = len(json.dumps(self.get("/api/state")))
        first = self.get("/api/state?since=")
        start = len(json.dumps(first))
        core.item_add(self.c, "a", "one more")
        poll = len(json.dumps(self.get(f"/api/state?since={first['v']}")))
        self.assertLess(start * 3, whole, (start, whole))
        self.assertLess(poll * 20, start, (poll, start))

    def test_versions_stay_small_and_unchanged_state_keeps_its_version(self):
        a = statedelta.answer({"items": [{"id": 1}], "x": 1})
        b = statedelta.answer({"items": [{"id": 1}], "x": 1}, a["v"])
        self.assertEqual((b["v"], b["parts"], b["rows"]), (a["v"], {}, {}))
        c = statedelta.answer({"items": [{"id": 1}], "x": 2}, a["v"])
        self.assertEqual(c["parts"], {"x": 2})
        self.assertIs(statedelta._versions[c["v"]]["items"], statedelta._versions[a["v"]]["items"],
                      "an unchanged part is one object in both versions")
        d = statedelta.answer({"items": [{"id": 1}]}, c["v"])
        self.assertEqual(d["drop"], ["x"])


@unittest.skipUnless(shutil.which("node"), "node is not on this machine")
class PageApplies(Base):
    """The page's own code (static/state.js) applies the answers."""

    STATIC = Path(server.__file__).parent / "static"

    def node(self, old, answers):
        code = (self.STATIC / "state.js").read_text(encoding="utf-8") + f"""
let s = {json.dumps(old)};
for (const a of {json.dumps(answers)}) s = applyAnswer(s, a);
console.log(JSON.stringify(s));
"""
        r = subprocess.run(["node", "--input-type=module"], input=code, capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_state_js_rebuilds_the_state(self):
        first = self.get("/api/state?since=")
        core.claim(self.c, self.one, "ag")
        core.item_add(self.c, "a", "three", priority=0)
        core.drop(self.c, self.two, actor="ag")
        a1 = self.get(f"/api/state?since={first['v']}&closed=1")
        core.register(self.c, "other")
        a2 = self.get(f"/api/state?since={a1['v']}&closed=1")
        page = self.node(None, [first, a1, a2])
        whole = self.get("/api/state?since=&closed=1")["state"]
        for part in ("now", "loop"):
            page.pop(part), whole.pop(part)
        self.assertEqual(page, whole)

    def test_the_client_asks_one_at_a_time(self):
        code = (self.STATIC / "state.js").read_text(encoding="utf-8") + """
const asked = [];
let n = 0;
const c = makeStateClient(async (u) => { asked.push(u); await new Promise(r => setTimeout(r, 10));
  n += 1; return n === 1 ? { v: "v1", state: { items: [{ id: 1 }] } } : { v: "v" + n, since: "v" + (n - 1), parts: {}, rows: { items: { set: [{ id: n }], order: [1, n] } } }; });
const all = await Promise.all([c.load(false), c.load(true), c.load(false)]);
console.log(JSON.stringify({ asked, last: all[2], v: c.version() }));
"""
        r = subprocess.run(["node", "--input-type=module"], input=code, capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["asked"], ["api/state?since=", "api/state?since=v1&closed=1", "api/state?since=v2"])
        self.assertEqual(out["last"], {"items": [{"id": 1}, {"id": 3}]})
        self.assertEqual(out["v"], "v3")


if __name__ == "__main__":
    unittest.main()
