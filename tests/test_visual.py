"""The visual check plugin (#5462): screenshots at set widths and themes against approved pictures."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from river import browser, cli, core
from river.core import RiverError
from river.plugins import visual


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        env = mock.patch.dict(os.environ, {"MAXPM_DB": os.path.join(self.dir.name, "t.db")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MAXPM_AGENT", None)
        self.c = core.connect()
        self.addCleanup(self.c.close)
        core.register(self.c, "mark", human=True)
        core.register(self.c, "w")
        core.register(self.c, "boss")
        self.c.execute("UPDATE agents SET role='manager' WHERE name='boss'")
        self.c.commit()


class Fake(Base):
    """The run, the lines, show and approve, with a fake Chrome: a picture is its page's text, and two pictures
    differ in the share of their bytes that differ."""

    def setUp(self):
        super().setUp()
        self.page = {"text": "home"}
        self.shots = []

        def shoot(url, shots, opts):
            if self.page["text"] is None:
                raise RiverError("net::ERR_CONNECTION_REFUSED")
            self.shots.append((url, [s for s, _, _ in shots], opts))
            return {s: f"{self.page['text']}|{s}".encode().ljust(100, b".") for s, _, _ in shots}

        def compare(a, b):
            n = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
            return {"ratio": n / max(len(a), len(b)), "diff_png": b"diff", "size": [1, 1, 1, 1]}
        for name, f in (("SHOOT", shoot), ("COMPARE", compare)):
            p = mock.patch.object(visual, name, f)
            p.start()
            self.addCleanup(p.stop)

    def cli(self, *words):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.run(list(words))
        return code, out.getvalue()

    def test_a_first_run_asks_for_approval_then_a_run_matches_and_a_change_is_critical(self):
        res = visual.run("home", "https://shop.example/", widths="1440,390", themes="light,dark", masks=[".clock"])
        self.assertEqual(res["status"], 1)
        self.assertEqual(list(res["shots"]), ["1440-light", "1440-dark", "390-light", "390-dark"])
        self.assertEqual(self.shots[0][2]["masks"], [".clock"])
        d = visual.folder("home")
        self.assertTrue((d / "new-1440-dark.png").exists())
        self.assertFalse((d / "base-1440-dark.png").exists())
        lines = visual.lines(res)
        self.assertTrue(lines[0].startswith("VISUAL WARNING - no approved picture of 1440-light, 1440-dark"))
        self.assertIn("maxpm ops visual approve home", lines[0])
        # The plugin check of maxpm serve reads one subject for each shot.
        subjects = core._ops_subjects(lines[1:], "warning")
        self.assertEqual(subjects["390-dark"], {"status": "warning", "text": "no approved picture yet"})
        self.assertEqual(json.loads(lines[-1])["approve"], "maxpm ops visual approve home")

        visual.approve(self.c, "home", actor="mark", note="first pictures")
        self.assertTrue((d / "base-1440-dark.png").exists())
        res = visual.run("home", "https://shop.example/")
        self.assertEqual((res["status"], res["perf"].split()[0]), (0, "1440-light=0.0%;0.01"))
        self.assertTrue(visual.lines(res)[0].startswith("VISUAL OK - 4 shots match the approved ones"))

        self.page["text"] = "HOME"
        res = visual.run("home", "https://shop.example/", widths="390", themes="dark")
        self.assertEqual(res["status"], 2)
        lines = visual.lines(res)
        self.assertTrue(lines[0].startswith("VISUAL CRITICAL - 1 of 1 shots differ from the approved ones: 390-dark 4.00%"))
        self.assertEqual(core._ops_subjects(lines[1:], "critical")["390-dark"]["status"], "critical")
        self.assertEqual(Path(res["shots"]["390-dark"]["diff_image"]).read_bytes(), b"diff")

    def test_the_page_failing_is_unknown(self):
        self.page["text"] = None
        res = visual.run("home", "https://shop.example/")
        self.assertEqual(res["status"], 3)
        self.assertIn("ERR_CONNECTION_REFUSED", visual.lines(res)[0])

    def test_only_a_person_or_the_manager_approves_and_the_history_says_so(self):
        visual.run("home", "https://shop.example/")
        with self.assertRaisesRegex(RiverError, "w is an agent; only a person or the manager approves"):
            visual.approve(self.c, "home", actor="w")
        visual.approve(self.c, "home", ["390-light"], actor="boss", note="the new header")
        d = visual.folder("home")
        self.assertEqual(sorted(p.name for p in d.glob("base-*")), ["base-390-light.png"])
        with self.assertRaisesRegex(RiverError, "no shot 800-dark"):
            visual.approve(self.c, "home", ["800-dark"], actor="mark")
        ev = self.c.execute("SELECT actor, change FROM events WHERE change LIKE 'ops visual%'").fetchall()
        self.assertEqual([tuple(r) for r in ev], [("boss", "ops visual home: approved the pictures of 390-light (was new) "
                                                           "(the new header) (by manager boss)")])

    def test_bad_settings_are_refused(self):
        for kw, text in (({"widths": "wide"}, "bad --widths"), ({"widths": "50"}, "from 200 to 4000"),
                         ({"themes": "blue"}, "bad --themes"), ({"threshold": 120}, "--threshold")):
            with self.assertRaisesRegex(RiverError, text):
                visual.run("home", "https://shop.example/", **kw)
        with self.assertRaisesRegex(RiverError, "lower-case"):
            visual.run("Home Page", "https://shop.example/")
        with self.assertRaisesRegex(RiverError, "has not run yet"):
            visual.show("other")

    def test_the_command_prints_the_plugin_lines_and_exits_with_the_status(self):
        code, out = self.cli("ops", "visual", "run", "home", "--url", "https://shop.example/", "--widths", "390",
                             "--themes", "light", "--mask", ".clock", "--full")
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("VISUAL WARNING"))
        self.assertTrue(self.shots[-1][2]["full"])
        code, out = self.cli("--as", "mark", "ops", "visual", "approve", "home")
        self.assertIn("home: approved the new pictures of 390-light", out)
        code, out = self.cli("ops", "visual", "run", "home", "--url", "https://shop.example/", "--widths", "390",
                             "--themes", "light")
        self.assertEqual(code, 0)
        code, out = self.cli("ops", "visual", "show", "home")
        self.assertIn("390-light: 0.00% differ", out)
        self.assertIn("approved: ", out)
        self.assertIn("maxpm ops visual approve home", out)

    def test_a_plugin_check_runs_it(self):
        core.project_add(self.c, "shop")
        r = core.ops_add(self.c, "shop", "look", "plugin", {
            "command": "maxpm ops visual run shop-home --url https://shop.example/", "subject": True,
            "timeout": "2m", "every": "1h"}, actor="mark")
        self.assertEqual(r["check"]["form"], "plugin")


@unittest.skipUnless(browser.chrome_path(), "no Chrome on this computer (MAXPM_CHROME names one)")
class RealChrome(Base):
    """A real headless Chrome: the same page matches, a changed text differs, a mask hides a clock."""

    def page(self, text, clock):
        html = (f"<!doctype html><style>body{{font:16px sans-serif;margin:20px}}@media (prefers-color-scheme: dark)"
                f"{{body{{background:#111;color:#eee}}}}</style><h1>Hello</h1><p class=clock>{clock}</p><p>{text}</p>")
        return "data:text/html," + quote(html)

    def test_run_approve_match_and_differ(self):
        kw = {"widths": "600", "themes": "light,dark", "masks": [".clock"], "wait": 0.2}
        self.assertEqual(visual.run("t", self.page("Some text.", "12:00"), **kw)["status"], 1)
        visual.approve(self.c, "t", actor="mark")
        res = visual.run("t", self.page("Some text.", "13:59"), **kw)
        self.assertEqual(res["status"], 0, visual.lines(res)[0])
        self.assertEqual(res["shots"]["600-dark"]["diff"], 0.0)
        res = visual.run("t", self.page("Other words, a longer line here.", "12:00"), **kw)
        self.assertEqual(res["status"], 2, visual.lines(res)[0])
        self.assertGreater(res["shots"]["600-light"]["diff"], 0.01)
        self.assertTrue(Path(res["shots"]["600-light"]["diff_image"]).read_bytes().startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
