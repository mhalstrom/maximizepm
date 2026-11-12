"""A new MaximizePM release (#5601): the daily check of GitHub, what the page shows, the app's code Update, the
launcher, and the manager's line."""

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from river import __version__, core, release, server
from river.core import RiverError

REPO = Path(__file__).resolve().parent.parent
NEXT = "9.9.9"


def code_archive(version=NEXT, broken=False, extra=None):
    """A code archive as desktop-release.yml makes it: one folder with bin and river, river saying version."""
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as d:
        top = Path(d) / f"maximizepm-{version}"
        shutil.copytree(REPO / "bin", top / "bin")
        shutil.copytree(REPO / "river", top / "river", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (top / "river" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        if broken:
            (top / "river" / "notify.py").write_text("this is not python\n", encoding="utf-8")
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            t.add(top, arcname=top.name)
            if extra:
                info = tarfile.TarInfo(extra)
                info.size = 1
                t.addfile(info, io.BytesIO(b"x"))
    return buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.c = core.connect(str(self.d / "t.db"))
        self.addCleanup(self.c.close)
        self.calls = []
        self.files = {}
        self.latest = {"tag_name": f"v{NEXT}", "html_url": f"https://github.com/{release.SLUG}/releases/tag/v{NEXT}",
                       "body": "## What is new\n\n- **Updates** in one click\n\n## Download\n\n- the .dmg files",
                       "assets": [{"name": "manifest.json", "browser_download_url": "https://dl/manifest.json"},
                                  {"name": f"MaximizePM-{NEXT}-code.tar.gz", "browser_download_url": "https://dl/code"},
                                  {"name": f"MaximizePM-{NEXT}-arm64.dmg", "browser_download_url": "https://dl/arm"},
                                  {"name": f"MaximizePM-{NEXT}-x64.dmg", "browser_download_url": "https://dl/x64"}]}

        def fetch(url, timeout):
            self.calls.append(url)
            if url == release.LATEST:
                return json.dumps(self.latest).encode()
            if url in self.files:
                return self.files[url]
            raise OSError(f"no such url {url}")
        old = release.FETCH
        release.FETCH = fetch
        self.addCleanup(setattr, release, "FETCH", old)
        env = mock.patch.dict(os.environ, {"MAXPM_APP_DIR": str(self.d / "app")})
        env.start()
        self.addCleanup(env.stop)

    def manifest(self, data, shell="shell-1"):
        self.files["https://dl/code"] = data
        self.files["https://dl/manifest.json"] = json.dumps({
            "version": NEXT, "shell": shell,
            "code": {"name": f"MaximizePM-{NEXT}-code.tar.gz", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}).encode()

    def as_app(self, shell="shell-1"):
        env = mock.patch.dict(os.environ, {"MAXPM_DESKTOP": "1", "MAXPM_APP_BUNDLE": str(self.d / "bundle"),
                                           "MAXPM_APP_SHELL": shell})
        env.start()
        self.addCleanup(env.stop)


class Check(Base):
    def test_versions(self):
        self.assertTrue(release.newer("v1.10.0", "1.9.9"))
        self.assertFalse(release.newer("1.0.0", "1.0.0"))
        self.assertFalse(release.newer("main", "1.0.0"))
        self.assertEqual(release.version_tuple("v2.3.4"), (2, 3, 4))

    def test_a_newer_release_is_kept_once_a_day_with_its_notes_and_manifest(self):
        self.manifest(b"code")
        st = release.check(self.c)
        self.assertIsNone(st["error"])
        self.assertEqual(st["latest"]["version"], NEXT)
        self.assertEqual(st["manifest"]["version"], NEXT)
        self.assertEqual(st["latest"]["notes"], "## What is new\n\n- **Updates** in one click")  # no Download part
        self.assertTrue((self.d / release.FILE).exists())
        n = len(self.calls)
        release.check(self.c)  # within the day: no request
        self.assertEqual(len(self.calls), n)
        release.check(self.c, force=True)
        self.assertGreater(len(self.calls), n)

    def test_an_error_is_kept_and_tried_again_after_an_hour(self):
        self.latest = {"tag_name": "nightly"}
        st = release.check(self.c)
        self.assertIn("no version tag", st["error"])
        self.assertFalse(release.status(self.c)["newer"])
        data = release.read(self.c)
        data["checked_at"] = time.time() - release.AFTER_ERROR - 1
        release._write(self.c, data)
        self.latest = {"tag_name": f"v{NEXT}", "assets": []}
        self.assertIsNone(release.check(self.c)["error"])

    def test_off_asks_nobody(self):
        core.config_set(self.c, "update_check", "off")
        release.check(self.c)
        self.assertEqual(self.calls, [])

    def test_the_same_version_is_not_new(self):
        self.latest["tag_name"] = f"v{__version__}"
        release.check(self.c)
        self.assertFalse(release.status(self.c)["newer"])
        self.assertNotIn("https://dl/manifest.json", self.calls)  # no manifest read for an old release
        self.assertIsNone(release.notice(self.c))


class Status(Base):
    def test_a_clone_updates_with_git(self):
        self.manifest(b"code")
        release.check(self.c)
        with mock.patch.object(release, "install_kind", return_value="git"):
            st = release.status(self.c)
        self.assertEqual((st["newer"], st["how"], st["version"], st["current"]), (True, "git", NEXT, __version__))

    def test_the_app_swaps_its_code_when_the_shell_is_the_same_else_offers_the_dmg(self):
        self.manifest(b"code", shell="shell-1")
        release.check(self.c)
        self.as_app("shell-1")
        self.assertEqual(release.status(self.c)["how"], "code")
        os.environ["MAXPM_APP_SHELL"] = "shell-0"
        st = release.status(self.c)
        self.assertEqual(st["how"], "shell")
        self.assertIn(st["dmg"], (f"MaximizePM-{NEXT}-arm64.dmg", f"MaximizePM-{NEXT}-x64.dmg"))

    def test_a_release_with_no_manifest_needs_the_new_app(self):
        self.latest["assets"] = [a for a in self.latest["assets"] if a["name"] != "manifest.json"]
        release.check(self.c)
        self.as_app()
        self.assertEqual(release.status(self.c)["how"], "shell")

    def test_the_manager_hears_of_it(self):
        self.manifest(b"code")
        release.check(self.c)
        self.assertIn(f"MaximizePM {NEXT} is out", release.notice(self.c))
        b = core.manage(self.c, str(self.d))
        self.assertIn(NEXT, b["release"])
        import contextlib
        from river import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.render_manage(b)
        self.assertIn("NEW VERSION: MaximizePM 9.9.9 is out", out.getvalue())


class AppUpdate(Base):
    def restart(self, root):
        self.restarted.append(root)

    def setUp(self):
        super().setUp()
        self.restarted = []
        self.as_app()

    def test_update_unpacks_checks_switches_and_restarts(self):
        self.manifest(code_archive())
        release.check(self.c)
        old = self.d / "app" / "versions" / "0.0.1"
        old.mkdir(parents=True)  # an older copy: it goes
        res = release.apply_code(self.c, self.restart)
        root = self.d / "app" / "versions" / NEXT
        self.assertEqual((res["updated"], res["to"], self.restarted), (True, NEXT, [root]))
        self.assertTrue((root / "bin" / "maxpm").is_file())
        self.assertIn(NEXT, (root / "river" / "__init__.py").read_text())
        self.assertEqual((self.d / "app" / "current").read_text().strip(), str(root))
        self.assertEqual((self.d / "app" / "trying").read_text().strip(), str(root))
        self.assertFalse(old.exists())
        self.assertEqual([p.name for p in (self.d / "app" / "versions").iterdir()], [NEXT])  # no temp folder left

    def test_a_wrong_checksum_changes_nothing(self):
        self.manifest(code_archive())
        self.files["https://dl/code"] = code_archive(extra="maximizepm-9.9.9/x")
        release.check(self.c)
        with self.assertRaisesRegex(RiverError, "does not match its checksum"):
            release.apply_code(self.c, self.restart)
        self.assertFalse((self.d / "app" / "current").exists())
        self.assertEqual(self.restarted, [])

    def test_code_that_does_not_load_stays_out(self):
        self.manifest(code_archive(broken=True))
        release.check(self.c)
        with self.assertRaisesRegex(RiverError, f"stays on {__version__}"):
            release.apply_code(self.c, self.restart)
        self.assertFalse((self.d / "app" / "versions" / NEXT).exists())
        self.assertFalse((self.d / "app" / "current").exists())
        self.assertEqual(self.restarted, [])

    def test_an_archive_with_a_path_outside_its_folder_is_refused(self):
        self.manifest(code_archive(extra="../evil"))
        release.check(self.c)
        with self.assertRaisesRegex(RiverError, "does not unpack"):
            release.apply_code(self.c, self.restart)
        self.assertFalse((self.d / "app" / "evil").exists())

    def test_a_new_shell_offers_the_dmg_and_not_the_code(self):
        self.manifest(code_archive(), shell="shell-2")
        release.check(self.c)
        with self.assertRaisesRegex(RiverError, "needs the new app"):
            release.apply_code(self.c, self.restart)
        self.files["https://dl/arm"] = self.files["https://dl/x64"] = b"dmg"
        opened = []
        with mock.patch.dict(os.environ, {"HOME": str(self.d)}):
            res = release.download_shell(self.c, opener=opened.append)
        self.assertEqual(Path(res["path"]).read_bytes(), b"dmg")
        self.assertEqual(opened, [Path(res["path"])])

    def test_the_new_serve_clears_trying(self):
        release.set_current(release.REPO)
        release.started()
        self.assertFalse((self.d / "app" / "trying").exists())
        self.assertTrue((self.d / "app" / "current").exists())

    def test_the_page_op_runs_the_app_update(self):
        self.manifest(code_archive())
        release.check(self.c)
        res = server.release_update(self.c, restart=self.restart)
        self.assertEqual((res["kind"], res["updated"]), ("app", True))

    def test_the_launcher_runs_the_current_version_and_does_not_change_after_an_update(self):
        with mock.patch.object(server, "DESKTOP", True):
            text = server._launcher_text()
            self.assertIn('"$d/current"', text)
            self.assertIn(str(self.d / "bundle" / "bin" / "maxpm"), text)
            self.assertNotIn(str(REPO / "bin"), text)


class Page(Base):
    def test_api_update_carries_the_release(self):
        self.manifest(b"code")
        release.check(self.c)
        sent = {}
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.headers = "/api/update?fetch=0", ("127.0.0.1", 1), {"Host": "127.0.0.1"}
        h._send = lambda code, body, *a, **k: sent.update(code=code, body=body)
        with mock.patch.dict(os.environ, {"MAXPM_DB": str(self.d / "t.db")}), \
                mock.patch.object(server, "update_status", return_value={"git": True}):
            h.do_GET()
        self.assertEqual(sent["code"], 200)
        self.assertEqual(sent["body"]["release"]["version"], NEXT)

    def test_an_install_that_is_not_the_app_or_a_clone_is_told_how(self):
        release.check(self.c)
        with mock.patch.object(release, "install_kind", return_value="other"):
            with self.assertRaisesRegex(RiverError, "the way you installed it"):
                server.release_update(self.c)


if __name__ == "__main__":
    unittest.main()
