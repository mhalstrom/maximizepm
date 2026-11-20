"""Build folders of sessions (#6398): an ended session's own build folder and the Cargo targets in its Claude Code
scratchpad go; a live session's, a person's own Claude Code session's, and a link to the project's target stay."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from river import core, scratch, server

OLD = time.time() - 3600


def make_target(d, tag=True):
    """A fake Cargo target folder of a few files."""
    (d / "debug" / "deps").mkdir(parents=True)
    (d / "debug" / "deps" / "libx.rlib").write_bytes(b"x" * 5000)
    if tag:
        (d / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    return d


class Builds(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        env = mock.patch.dict(os.environ, {"MAXPM_DB": str(self.root / "t.db")})
        env.start()
        self.addCleanup(env.stop)
        self.c = core.connect(str(self.root / "t.db"))
        self.addCleanup(self.c.close)
        self.builds = self.root / "builds"
        core.config_set(self.c, "session_builds", str(self.builds))
        self.tmp = self.root / "claude-501"
        p = mock.patch.object(server, "CLAUDE_TMP", str(self.tmp))
        p.start()
        self.addCleanup(p.stop)
        self.project_target = make_target(self.root / "project" / "target")

    def session(self, name, sid, ended=True):
        """A session with a scratchpad that holds two targets, other files and a link to the project's target."""
        core.register(self.c, name)
        with core.tx(self.c):
            self.c.execute("INSERT INTO agent_sessions (agent, session_id, first_seen) VALUES (?,?,?)",
                           (name, sid, core.iso(core.now())))
        pad = self.tmp / "-Users-x-Projects-p" / sid / "scratchpad"
        make_target(pad / "clean6363" / "target")
        make_target(pad / "t6331-target", tag=False)
        (pad / "clean6363" / "src.rs").parent.mkdir(parents=True, exist_ok=True)
        (pad / "clean6363" / "src.rs").write_text("fn main() {}")
        (pad / "shot.png").write_bytes(b"png")
        os.symlink(self.project_target, pad / "target")
        b = scratch.build_dir(self.c, name, make=True)
        make_target(b / "export")
        os.utime(b, (OLD, OLD))
        if ended:
            core.unregister(self.c, name, "t")
        return pad, b

    def test_ended_session_loses_its_build_folders(self):
        pad, b = self.session("w-1", "sid-1")
        res = scratch.sweep(self.c)
        self.assertFalse(b.exists())
        self.assertFalse((pad / "clean6363" / "target").exists())
        self.assertFalse((pad / "t6331-target").exists())
        self.assertTrue((pad / "clean6363" / "src.rs").exists())
        self.assertTrue((pad / "shot.png").exists())
        self.assertTrue((pad / "target").is_symlink())  # the link stays, and the project's target with it
        self.assertTrue((self.project_target / "debug" / "deps" / "libx.rlib").exists())
        self.assertEqual(res["w-1"]["folders"], 2)
        self.assertGreater(res["w-1"]["bytes"], 0)
        line = self.c.execute("SELECT change FROM events WHERE change LIKE 'w-1 ended: removed%'").fetchone()[0]
        self.assertIn("its build folder", line)
        self.assertIn("2 build folders in its scratchpad", line)
        self.assertEqual(scratch.sweep(self.c), {})  # walked once: the next pass has nothing

    def test_live_session_and_unknown_folders_stay(self):
        pad, b = self.session("w-2", "sid-2", ended=False)
        other = self.tmp / "-Users-x-Projects-p" / "persons-own" / "scratchpad"
        make_target(other / "target")
        stranger = make_target(self.builds / "other-queue" / "t")
        os.utime(stranger.parent, (OLD, OLD))
        self.assertEqual(scratch.sweep(self.c), {})
        self.assertTrue((b / "export").exists())
        self.assertTrue((pad / "clean6363" / "target").exists())
        self.assertTrue((other / "target").exists())
        self.assertTrue(stranger.exists())

    def test_scratchpads_are_walked_once_in_scan_every(self):
        scratch.sweep(self.c)  # nothing yet: the walk is done for SCAN_EVERY
        pad, b = self.session("w-7", "sid-7")
        res = scratch.sweep(self.c)
        self.assertFalse(b.exists())  # the build folder goes at each pass
        self.assertEqual(res["w-7"]["folders"], 0)
        self.assertTrue((pad / "t6331-target").exists())
        later = core.now() + scratch.SCAN_EVERY
        with mock.patch.object(core, "now", return_value=later):
            res = scratch.sweep(self.c)
        self.assertEqual(res["w-7"]["folders"], 2)
        self.assertFalse((pad / "t6331-target").exists())

    def test_a_new_build_folder_stays(self):
        _, b = self.session("w-3", "sid-3")
        os.utime(b, None)  # made just now: a session that starts
        scratch.sweep(self.c)
        self.assertTrue(b.exists())

    def test_dry_run_removes_nothing(self):
        pad, b = self.session("w-4", "sid-4")
        res = scratch.sweep(self.c, dry_run=True)
        self.assertEqual(res["w-4"]["folders"], 2)
        self.assertTrue(b.exists())
        self.assertTrue((pad / "t6331-target").exists())

    def test_start_names_the_build_folder_in_the_pane_environment(self):
        from tests.test_server import FakeTmux
        tmux = FakeTmux()
        p = mock.patch.object(server, "TMUX_RUNNER", tmux)
        p.start()
        self.addCleanup(p.stop)
        core.project_add(self.c, "web", path=str(self.root))
        server._open_terminal({"path": str(self.root), "command": "claude", "project": "web"},
                              {"MAXPM_AGENT": "w-5"}, conn=self.c)
        made = next(a for a in tmux.calls if a[0] in ("new-session", "new-window", "split-window"))
        env = [made[i + 1] for i, x in enumerate(made) if x == "-e"]
        self.assertIn(f"MAXPM_BUILD_DIR={self.builds / 'w-5'}", env)
        self.assertNotIn("MAXPM_BUILD_DIR", repr(tmux.panes[-1].get("typed")))  # the typed line stays short

    def test_briefing_says_where_builds_go(self):
        import contextlib
        import io
        from river import cli
        core.project_add(self.c, "web", path=str(self.root))
        iid = core.item_add(self.c, "web", "a task")["id"]
        b = core.go(self.c, str(self.root), "w-6", focus=f"item:{iid}")
        self.assertIsNone(b["build_dir"])  # a session that maxpm did not start has none
        d = scratch.build_dir(self.c, "w-6", make=True)
        b = core.go(self.c, str(self.root), "w-6")
        self.assertEqual(b["build_dir"], str(d))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_go(b)
        self.assertIn(f"CARGO_TARGET_DIR=$MAXPM_BUILD_DIR/<name> ({d})", out.getvalue())


if __name__ == "__main__":
    unittest.main()
