"""The Changes view (#5855): a session's diff against GitHub main, not committed, or since the last review; an item's
commits; comments to the session; Approve and Request changes; the push and release gates of review_at."""

import os
import subprocess
import tempfile
import unittest

from river import core, review
from river.core import RiverError


class Base(unittest.TestCase):
    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def write(self, name, text):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as f:
            f.write(text)

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.repo = os.path.realpath(os.path.join(self.dir.name, "app"))
        os.makedirs(self.repo)
        self.git("init", "-q", "-b", "main")
        self.write("a.py", "one\ntwo\nthree\n")
        self.base = self.commit("start")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")  # what GitHub has
        self.c = core.connect(os.path.join(self.dir.name, "t.db"))
        self.addCleanup(self.c.close)
        core.project_add(self.c, "app", path=self.repo)
        core.register(self.c, "mark", human=True)
        core.register(self.c, "dev")
        self.item = core.item_add(self.c, "app", "change a", actor="t")["id"]
        core.claim(self.c, self.item, "dev")  # its folder: the project's, through the item it holds


class SessionDiff(Base):
    def test_against_github_main_holds_commits_edits_and_new_files(self):
        self.write("a.py", "one\nTWO\nthree\n")
        self.commit(f"Change two (#{self.item})")
        self.write("a.py", "one\nTWO\nthree\nfour\n")  # not committed
        self.write("new.txt", "hello\n")              # not known to git
        d = review.session_diff(self.c, "dev")
        self.assertEqual(d["root"], self.repo)
        self.assertIn("origin/main", d["against"])
        files = {f["path"]: f for f in d["files"]}
        self.assertEqual(set(files), {"a.py", "new.txt"})
        self.assertEqual((files["a.py"]["add"], files["a.py"]["del"]), (2, 1))
        rows = files["a.py"]["hunks"][0]["lines"]
        self.assertIn(["-", 2, None, "two"], rows)
        self.assertIn(["+", None, 2, "TWO"], rows)
        self.assertIn(["+", None, 4, "four"], rows)
        self.assertTrue(files["new.txt"]["untracked"])
        self.assertEqual(files["new.txt"]["hunks"][0]["lines"], [["+", None, 1, "hello"]])
        self.assertEqual(d["commits_ahead"], 1)
        self.assertEqual(review.count_of(self.c, "dev"), 2)

    def test_not_committed_shows_only_the_edits_after_the_last_commit(self):
        self.write("a.py", "one\nTWO\nthree\n")
        self.commit("c1")
        self.write("a.py", "one\nTWO\nthree\nfour\n")
        d = review.session_diff(self.c, "dev", "uncommitted")
        rows = [r for h in d["files"][0]["hunks"] for r in h["lines"] if r[0] != " "]
        self.assertEqual(rows, [["+", None, 4, "four"]])

    def test_since_my_last_review(self):
        self.write("a.py", "one\nTWO\nthree\n")
        self.commit("c1")
        review.mark(self.c, "reviewed", agent="dev", actor="mark")
        d = review.session_diff(self.c, "dev", "review")
        self.assertEqual(d["files"], [])  # nothing since
        self.write("b.py", "x\n")
        self.commit("c2")
        d = review.session_diff(self.c, "dev", "review")
        self.assertEqual([f["path"] for f in d["files"]], ["b.py"])
        self.assertIn("your last review", d["against"])

    def test_a_long_diff_is_cut_and_keeps_its_counts(self):
        self.write("big.txt", "".join(f"line {i}\n" for i in range(50)))
        self.commit("big")
        with unittest.mock.patch.object(review, "MAX_LINES", 10):
            d = review.session_diff(self.c, "dev")
        f = d["files"][0]
        self.assertEqual(f["add"], 50)
        self.assertTrue(f["cut"] and d["cut"])
        self.assertEqual(sum(len(h["lines"]) for h in f["hunks"]), 10)

    def test_a_session_in_the_same_folder_is_named(self):
        core.register(self.c, "dev2")
        it = core.item_add(self.c, "app", "other", actor="t")["id"]
        core.claim(self.c, it, "dev2")
        self.assertEqual(review.session_diff(self.c, "dev")["shared"], ["dev2"])

    def test_bad_base_and_unknown_session(self):
        with self.assertRaises(RiverError):
            review.session_diff(self.c, "dev", "yesterday")
        with self.assertRaises(RiverError):
            review.session_diff(self.c, "nobody")


class ItemDiff(Base):
    def test_the_commits_that_name_the_item_also_after_the_push(self):
        self.write("a.py", "one\nTWO\nthree\n")
        first = self.commit(f"Change two (#{self.item})")
        self.write("c.py", "c\n")
        self.commit(f"Other work (#{self.item}0)")  # #<id>0 is another item
        self.write("a.py", "one\nTWO\nTHREE\n")
        second = self.commit(f"Change three\n\nFor #{self.item}.")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")  # pushed
        d = review.item_diff(self.c, self.item)
        self.assertEqual([c["sha"] for c in d["commits"]], [first, second])
        self.assertEqual(d["commits"][0]["files"][0]["path"], "a.py")


class Answers(Base):
    def test_comments_go_to_the_session_as_one_alert_with_file_and_line(self):
        review.send(self.c, [{"file": "a.py", "line": 2, "quote": "TWO", "text": "Keep it lower case."},
                             {"file": "a.py", "line": 3, "end": 5, "text": "Explain this part."}], agent="dev",
                    actor="mark")
        m = self.c.execute("SELECT * FROM messages WHERE to_agent='dev' AND kind='alert'").fetchone()
        self.assertEqual(m["from_agent"], "mark")
        self.assertIn("- a.py:2 (`TWO`): Keep it lower case.", m["body"])
        self.assertIn("- a.py:3-5: Explain this part.", m["body"])
        with self.assertRaises(RiverError):
            review.send(self.c, [], agent="dev", actor="mark")

    def test_push_waits_for_approve_when_the_project_says_so(self):
        cmd = "git push origin HEAD"
        self.assertIsNone(core.push_guard(self.c, "dev", cmd))  # review_at off
        core.config_set(self.c, "review_at", "push", project="app")
        self.write("a.py", "x\n")
        head = self.commit("c1")
        why = core.push_guard(self.c, "dev", cmd)
        self.assertIn("the person reviews the changes first", why)
        alert = self.c.execute("SELECT body FROM messages WHERE to_agent='mark' AND kind='alert'").fetchall()
        self.assertEqual(len(alert), 1)
        self.assertIn(f"/sessions/dev", alert[0][0])
        core.push_guard(self.c, "dev", cmd)  # one alert per commit
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM messages WHERE to_agent='mark'").fetchone()[0], 1)
        review.send(self.c, [{"file": "a.py", "line": 1, "text": "no"}], agent="dev", actor="mark", request=True)
        review.mark(self.c, "changes", agent="dev", actor="mark")
        self.assertIn("requested changes", core.push_guard(self.c, "dev", cmd))
        review.mark(self.c, "approved", agent="dev", actor="mark")
        self.assertIsNone(review.push_held(self.c, "dev", cmd))
        self.assertEqual(review.session_diff(self.c, "dev")["state"], "approved")
        self.write("a.py", "y\n")
        self.commit("c2")  # a new commit needs a new Approve
        self.assertIsNotNone(review.push_held(self.c, "dev", cmd))
        self.assertIn("alert", core.push_guard(self.c, "dev", "maxpm launch --item 3"))  # the unread comments
        core.inbox(self.c, "dev")
        self.assertIsNone(core.push_guard(self.c, "dev", "maxpm launch --item 3"))  # review holds only a push
        self.assertTrue(head)

    def test_a_cut_release_waits_for_approve(self):
        core.config_set(self.c, "review_at", "release", project="app")
        core.target_add(self.c, "prod", actor="mark")
        core.project_target(self.c, "app", "prod", actor="mark")
        core.done(self.c, self.item, "x", "dev", ship_it=True)
        dep = self.c.execute("SELECT id FROM items WHERE kind='deploy'").fetchone()
        with core.tx(self.c):
            core._release_cut(self.c, dep["id"], "mark", "test")
        it = core._item(self.c, dep["id"])
        self.assertEqual(it["blocked_set_by"], review.REVIEW_BY)
        self.assertTrue(self.c.execute("SELECT 1 FROM messages WHERE to_agent='mark' AND body LIKE ?",
                                       (f"Release #{dep['id']}%",)).fetchone())
        review.mark(self.c, "approved", item=dep["id"], actor="mark")
        self.assertIsNone(core._item(self.c, dep["id"])["blocked_reason"])

    def test_the_setting_takes_three_values(self):
        for v in ("off", "push", "release"):
            core.config_set(self.c, "review_at", v, project="app")
        with self.assertRaises(RiverError):
            core.config_set(self.c, "review_at", "always", project="app")


class Server(Base):
    """GET /api/diff and the ops review_send and review_mark of the page."""

    def get(self, path):
        from river import server
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.addCleanup(os.environ.pop, "MAXPM_DB", None)
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.headers = path, ("127.0.0.1", 1), {"Host": "127.0.0.1:8765"}
        got = {}
        h._send = lambda code, body, *a, **k: got.update(code=code, body=body)
        h.do_GET()
        return got

    def test_the_route_and_the_ops(self):
        from river import server
        self.write("a.py", "one\nTWO\nthree\n")
        self.commit(f"c (#{self.item})")
        got = self.get("/api/diff?session=dev&base=origin")
        self.assertEqual(got["code"], 200)
        self.assertEqual([f["path"] for f in got["body"]["files"]], ["a.py"])
        got = self.get(f"/api/diff?item={self.item}")
        self.assertEqual(len(got["body"]["commits"]), 1)
        self.assertEqual(self.get("/api/diff?session=nobody")["code"], 409)
        server.OPS["review_send"](self.c, {"session": "dev", "request": True, "comments": [
            {"file": "a.py", "line": 2, "text": "lower case"}]}, "mark")
        self.assertEqual(review.session_diff(self.c, "dev")["state"], "changes")
        server.OPS["review_mark"](self.c, {"session": "dev", "kind": "approved"}, "mark")
        self.assertEqual(review.session_diff(self.c, "dev")["state"], "approved")


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
