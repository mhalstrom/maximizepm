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

    def test_trim_after_done_removes_incremental_and_keeps_live_session_targets(self):
        """#6821: live sessions reuse worktrees across items, so trim removes only debug/incremental* and
        release/incremental* of a live session's targets (even when named after a done item), and keeps
        target/debug/deps, source files, test data, databases, and symlinks."""
        core.project_add(self.c, "local", path=str(self.root / "project"))
        core.register(self.c, "local-1")
        done_id = core.item_add(self.c, "local", "finished task")["id"]
        open_id = core.item_add(self.c, "local", "active task")["id"]
        core.claim(self.c, done_id, "local-1")
        core.done(self.c, done_id, "ok", actor="local-1")
        core.claim(self.c, open_id, "local-1")

        b = scratch.build_dir(self.c, "local-1", make=True)
        # 1. Worktree named after done item (reused by live session): target/debug/deps stays, incremental* goes.
        wt_done = b / f"wt-{done_id}"
        (wt_done / "src").mkdir(parents=True)
        (wt_done / "src" / "lib.rs").write_text("pub fn ok() {}")
        make_target(wt_done / "target")
        (wt_done / "target" / "debug" / "incremental" / "crate").mkdir(parents=True)
        (wt_done / "target" / "debug" / "incremental" / "crate" / "a.bin").write_bytes(b"1" * 1000)
        (wt_done / "target" / "debug" / "incremental.kache-auto" / "v1").mkdir(parents=True)
        (wt_done / "target" / "debug" / "incremental.kache-auto" / "v1" / "b.bin").write_bytes(b"2" * 1000)
        (wt_done / "target" / "release" / "incremental" / "r").mkdir(parents=True)
        (wt_done / "target" / "release" / "incremental" / "r" / "c.bin").write_bytes(b"3" * 1000)
        # 2. Test data folder / database named after done item: stays.
        live_data = b / f"live-{done_id}"
        live_data.mkdir()
        (live_data / "state.db").write_bytes(b"sqlite")
        # 3. Generic build folder: incremental* goes, deps and Cargo.toml stay.
        generic = b / "app-build"
        generic.mkdir()
        (generic / "Cargo.toml").write_text("[package]\nname='x'\n")
        make_target(generic / "target")
        (generic / "target" / "debug" / "incremental" / "u").mkdir(parents=True)
        (generic / "target" / "debug" / "incremental" / "u" / "x.bin").write_bytes(b"4" * 1000)
        # 4. Symlink to project target: stays untouched.
        os.symlink(self.project_target, b / "link-target")

        # Dry run removes nothing.
        dry = scratch.trim_session_builds(self.c, dry_run=True)
        self.assertIn("local-1", dry)
        self.assertTrue((wt_done / "target" / "debug" / "incremental").exists())

        res = scratch.trim_session_builds(self.c)
        self.assertIn("local-1", res)
        self.assertGreater(res["local-1"]["bytes"], 0)
        # Live session keeps wt-<done>/target and deps; loses only incremental*.
        self.assertTrue((wt_done / "target" / "debug" / "deps" / "libx.rlib").exists())
        self.assertFalse((wt_done / "target" / "debug" / "incremental").exists())
        self.assertFalse((wt_done / "target" / "debug" / "incremental.kache-auto").exists())
        self.assertFalse((wt_done / "target" / "release" / "incremental").exists())
        self.assertTrue((wt_done / "src" / "lib.rs").exists())
        self.assertTrue((live_data / "state.db").exists())
        # Generic build folder lost incremental*, kept deps and Cargo.toml.
        self.assertFalse((generic / "target" / "debug" / "incremental").exists())
        self.assertTrue((generic / "target" / "debug" / "deps" / "libx.rlib").exists())
        self.assertTrue((generic / "Cargo.toml").exists())
        # Symlink target stayed.
        self.assertTrue((self.project_target / "debug" / "deps" / "libx.rlib").exists())
        # Event logged.
        line = self.c.execute("SELECT change FROM events WHERE change LIKE 'local-1 trimmed: removed%'").fetchone()[0]
        self.assertIn("build output folder", line)
        # Idempotent until another item closes.
        self.assertEqual(scratch.trim_session_builds(self.c), {})

    def test_trim_skips_target_with_cargo_lock_or_running_process(self):
        """#6821: a target held by a cargo lock or running process is never trimmed."""
        import fcntl
        core.project_add(self.c, "local", path=str(self.root / "project"))
        core.register(self.c, "local-2")
        done_id = core.item_add(self.c, "local", "locked task")["id"]
        core.claim(self.c, done_id, "local-2")
        core.done(self.c, done_id, "ok", actor="local-2")

        b = scratch.build_dir(self.c, "local-2", make=True)
        wt = b / f"wt-{done_id}"
        t = make_target(wt / "target")
        inc = t / "debug" / "incremental" / "u"
        inc.mkdir(parents=True)
        (inc / "a.bin").write_bytes(b"1" * 1000)
        lock_file = t / "debug" / ".cargo-lock"
        lock_file.write_text("")

        with open(lock_file, "r+b") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                res = scratch.trim_session_builds(self.c, force=True)
                self.assertEqual(res, {})
                self.assertTrue(inc.exists())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

        # Simulated running process referencing the target directory also protects it.
        with mock.patch.object(scratch, "_ps_lines", return_value=[(999999, f"{t}/debug/my-test")]):
            res = scratch.trim_session_builds(self.c, force=True)
            self.assertEqual(res, {})
            self.assertTrue(inc.exists())

        # Once unlocked and idle, force=True trims incremental* and keeps target/debug/deps.
        with mock.patch.object(scratch, "_ps_lines", return_value=[]):
            res = scratch.trim_session_builds(self.c, force=True)
            self.assertIn("local-2", res)
            self.assertFalse((t / "debug" / "incremental").exists())
            self.assertTrue((t / "debug" / "deps" / "libx.rlib").exists())


if __name__ == "__main__":
    unittest.main()
