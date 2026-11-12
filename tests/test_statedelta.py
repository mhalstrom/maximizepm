"""The page's state as versions and changes (#4709): /api/state?since=<v> answers only what changed."""
import gzip
import json
import os
import unittest.mock
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
        h._send = lambda code, body, ctype=None: out.update(
            code=code, body=json.loads(body if isinstance(body, bytes) else json.dumps(body, default=str)))
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

    def test_without_since_the_state_of_the_first_answer(self):
        # A bare /api/state was every item with every field: 21.5 MB for a queue of 5,600 items (#5590).
        st = self.get("/api/state")
        self.assertNotIn("v", st)
        self.assertEqual([i["id"] for i in st["items"]], [self.one, self.two])
        self.assertNotIn("context", st["items"][0], "the drawer loads the item itself")
        done = self.get("/api/state?closed=1")
        self.assertEqual({i["id"] for i in done["items"]}, {self.one, self.two, self.old})

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
        whole = len(json.dumps(server.page_state(True), default=str))  # every item with every field
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
def wire(path, headers):
    """What the real Handler._send writes for a GET of path: (status line and headers as a dict, body bytes)."""
    import io
    h = server.Handler.__new__(server.Handler)
    h.path, h.client_address, h.headers = path, ("127.0.0.1", 5555), {"Host": "127.0.0.1:8765", **headers}
    h.wfile, h.request_version, h.requestline, h.command = io.BytesIO(), "HTTP/1.1", f"GET {path} HTTP/1.1", "GET"
    h.do_GET()
    head, _, body = h.wfile.getvalue().partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    return lines[0], {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}, body


class Gzip(Base):
    """The page's answers go gzipped to a browser that accepts gzip (#5590)."""

    def test_gzip_only_when_the_browser_takes_it(self):
        import gzip
        first, heads, body = wire("/api/state?since=", {"Accept-Encoding": "gzip, deflate, br"})
        self.assertIn(" 200 ", first)
        self.assertEqual(heads["content-encoding"], "gzip")
        self.assertEqual(heads["vary"], "Accept-Encoding")
        self.assertEqual(int(heads["content-length"]), len(body))
        st = json.loads(gzip.decompress(body))["state"]
        self.assertEqual([i["id"] for i in st["items"]], [self.one, self.two])
        _, heads, body = wire("/api/state?since=", {})
        self.assertNotIn("content-encoding", heads)
        self.assertIn("state", json.loads(body))
        _, heads, _ = wire("/api/state?since=", {"Accept-Encoding": "gzip;q=0"})
        self.assertNotIn("content-encoding", heads)

    def test_the_relay_gzips_for_itself(self):
        from river import relay
        _, heads, body = wire("/api/state?since=", {"Accept-Encoding": "gzip", "X-Maxpm-Relay": relay.PAGE_SECRET})
        self.assertNotIn("content-encoding", heads)
        self.assertIn("state", json.loads(body))

    def test_a_small_answer_goes_plain(self):
        _, heads, body = wire("/api/token", {"Accept-Encoding": "gzip"})
        self.assertNotIn("content-encoding", heads)
        self.assertIn("token", json.loads(body))


class Shared(Base):
    """One built state, many requests: the slim state, the row hashes, the encoded answer and its gzip are made
    once (#5591: the done view of 5,300 rows took 130 ms on each request, the build aside)."""

    def test_the_second_page_gets_the_same_bytes_without_new_work(self):
        server.STATE_TTL = 60
        self.addCleanup(setattr, server, "STATE_TTL", 0)
        server.state_changed()
        calls = {"slim": 0, "sig": 0}
        slim, sig = statedelta.slim, statedelta._signature

        def count(name, f):
            def run(*a):
                calls[name] += 1
                return f(*a)
            return run
        with unittest.mock.patch.object(statedelta, "slim", count("slim", slim)), \
                unittest.mock.patch.object(statedelta, "_signature", count("sig", sig)):
            _, _, one = wire("/api/state?since=&closed=1", {"Accept-Encoding": "gzip"})
            _, _, two = wire("/api/state?since=&closed=1", {"Accept-Encoding": "gzip"})
            v = json.loads(gzip.decompress(two))["v"]
            _, _, poll = wire(f"/api/state?since={v}&closed=1", {"Accept-Encoding": "gzip"})
        self.assertEqual(one, two)
        self.assertEqual(calls, {"slim": 1, "sig": 1})
        self.assertLess(len(poll), 1024, "a poll with no change goes plain")
        ans = json.loads(poll)
        self.assertEqual((ans["v"], ans["rows"]), (v, {}))
        # An action builds the state anew, and the answer follows it.
        core.item_add(self.c, "a", "three")
        server.state_changed()
        _, heads, three = wire(f"/api/state?since={v}&closed=1", {"Accept-Encoding": "gzip"})
        three = json.loads(gzip.decompress(three) if heads.get("content-encoding") else three)
        self.assertEqual([r["title"] for r in three["rows"]["items"]["set"]], ["three"])


class KeptDoneRows(Base):
    """A page build computes a done item again only when what it shows of it changed (#5592)."""

    def build(self):
        server.state_changed()
        st = server.page_state(True)
        return {i["id"]: i for i in st["items"]}

    def test_an_unchanged_done_item_is_not_computed_again(self):
        server._kept_graph.clear()
        self.addCleanup(server._kept_graph.clear)
        self.build()
        seen = []
        one = core._item_models

        def count(it, *a):
            seen.append(it["id"])
            return one(it, *a)
        with unittest.mock.patch.object(core, "_item_models", count):
            rows = self.build()
            self.assertNotIn(self.old, seen, "the done item was kept")
            self.assertIn(self.one, seen, "open items are computed as before")
            self.assertEqual(rows[self.old]["unblocks"], [])
            # A new item that waits on the done item: the done item now unblocks it.
            later = core.item_add(self.c, "a", "later", after=[self.old])["id"]
            seen.clear()
            rows = self.build()
            self.assertIn(self.old, seen)
            self.assertEqual(rows[self.old]["unblocks"], [later])
            # Its own row changed.
            core.item_edit(self.c, self.old, title="old, renamed", actor="ag")
            rows = self.build()
            self.assertEqual(rows[self.old]["title"], "old, renamed")
            # Its project's rank changed.
            core.project_add(self.c, "b")
            core.project_rank(self.c, "a", 2)
            rows = self.build()
            self.assertEqual(rows[self.old]["project_rank"], core.project_show(self.c, "a")["rank"])
            # Nothing changed: kept again.
            seen.clear()
            self.build()
            self.assertNotIn(self.old, seen)


    def test_a_done_item_sees_the_change_of_an_item_it_waits_on(self):
        # The mark holds the rows of the linked items (#5597): their status reaches the kept done row.
        server._kept_graph.clear()
        self.addCleanup(server._kept_graph.clear)
        first = core.item_add(self.c, "a", "first")["id"]
        late = core.item_add(self.c, "a", "late", after=[first])["id"]
        core.done(self.c, late, actor="ag", force="done before its prerequisite")
        self.assertEqual(self.build()[late]["open_blockers"], [first])
        self.build()
        core.claim(self.c, first, "ag")
        core.done(self.c, first, actor="ag")
        self.assertEqual(self.build()[late]["open_blockers"], [])

    def test_an_unchanged_done_row_reaches_the_answer_without_new_work(self):
        # #5593: the same done row from annotate to the slim row and its hash; a lone page's done-view poll was
        # 190 ms on 5,600 items, as state, slim and the hashes worked on every row of each build.
        server._kept_graph.clear()
        self.addCleanup(server._kept_graph.clear)
        first = self.build()
        st1 = server.page_state(True)
        slim1 = {r["id"]: r for r in statedelta.slim(st1)["items"]}
        server.state_changed()
        st2 = server.page_state(True)
        two = {r["id"]: r for r in st2["items"]}
        self.assertIs(two[self.old], {r["id"]: r for r in st1["items"]}[self.old], "the same state row")
        self.assertIsNot(two[self.one], first[self.one], "open rows are new each build")
        slim2 = {r["id"]: r for r in statedelta.slim(st2)["items"]}
        self.assertIs(slim2[self.old], slim1[self.old], "the same slim row")
        # A changed done item gets a new row, and the poll carries it.
        v = statedelta.answer(statedelta.slim(st2))["v"]
        core.item_edit(self.c, self.old, title="old, renamed", actor="ag")
        server.state_changed()
        st3 = server.page_state(True)
        self.assertEqual({r["id"]: r for r in st3["items"]}[self.old]["title"], "old, renamed")
        ans = statedelta.answer(statedelta.slim(st3), v)
        self.assertEqual([r["title"] for r in ans["rows"]["items"]["set"]], ["old, renamed"])
        # The open view between two done-view builds keeps the kept rows.
        server.state_changed()
        server.page_state(False)
        server.state_changed()
        st4 = server.page_state(True)
        self.assertIs({r["id"]: r for r in st4["items"]}[self.old], {r["id"]: r for r in st3["items"]}[self.old])


class Timing(Base):
    def test_each_state_answer_records_its_wait_and_build(self):
        server.STATE_TIMES.clear()
        wire("/api/state?since=&closed=1", {})
        wire("/api/state?since=", {})
        got = self.get("/api/state-timing")
        self.assertEqual([t["closed"] for t in got], [True, False])
        for t in got:
            self.assertGreaterEqual(t["total_ms"], t["build_ms"])
            self.assertIn("wait_ms", t)
            self.assertTrue(t["whole"])
            self.assertIn("cpu_ms", t)
            self.assertEqual(set(t["parts"]), {"start", "activity", "state", "goals", "terminals", "sessions", "rest"})


class Limits(Base):
    """The speed test of the page's data (#5590, p147): on a queue of the size of mark's (300 open items, 5,000 done,
    long notes and output), the first answer is under 1 MB gzipped, a poll after one change under 50 KB, and the
    done view under 3 MB gzipped. The numbers of 2026-11-12: 147 KB, 4 to 35 KB, 1.8 MB."""

    def setUp(self):
        super().setUp()
        pid = self.c.execute("SELECT id FROM projects WHERE name='a'").fetchone()[0]
        now = core.iso(core.now())
        text = "why it matters, where the code is, and what to check " * 20
        with core.tx(self.c):
            self.c.executemany(
                "INSERT INTO items (project_id, title, notes, context, status, created_at, rank) VALUES (?,?,?,?,?,?,?)",
                [(pid, f"open item {n} with a title of some length", text, text, "open", now, n) for n in range(300)])
            self.c.executemany(
                "INSERT INTO items (project_id, title, notes, context, output, status, assignee, created_at,"
                " closed_at, rank) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(pid, f"done item {n} with a title of some length", text, text, text, "done", "ag", now, now, n)
                 for n in range(5000)])

    def test_the_page_data_stays_under_the_limits(self):
        gz = {"Accept-Encoding": "gzip"}
        _, heads, first = wire("/api/state?since=", gz)
        self.assertEqual(heads["content-encoding"], "gzip")
        self.assertLess(len(first), 1024 * 1024, "the first answer, gzipped")
        import gzip
        v = json.loads(gzip.decompress(first))["v"]
        core.item_add(self.c, "a", "one more")
        _, _, poll = wire(f"/api/state?since={v}", gz)
        self.assertLess(len(poll), 50 * 1024, "a poll after one change")
        _, _, done = wire(f"/api/state?since={v}&closed=1", gz)
        self.assertLess(len(done), 3 * 1024 * 1024, "the done view, gzipped")
        _, _, bare = wire("/api/state", gz)
        self.assertLess(len(bare), 1024 * 1024, "a bare /api/state is the first answer's state")


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
