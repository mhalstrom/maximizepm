"""Dictation (#5107): maxpm serve runs the helper for each dictation of the page and gives back the text so far."""
import base64
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from river import core, dictation, server
from river.core import RiverError

# A helper that counts the audio it reads: a partial text after each piece, the final text at the end.
FAKE = textwrap.dedent('''
    import json, sys
    loc = sys.argv[sys.argv.index("--locale") + 1]
    say = lambda **o: (sys.stdout.write(json.dumps(o) + "\\n"), sys.stdout.flush())
    if loc == "xx-YY":
        say(error="macOS has no speech model for the language xx-YY"); sys.exit(1)
    say(ready=loc)
    n = 0
    while True:
        b = sys.stdin.buffer.read1(65536)
        if not b:
            break
        n += len(b)
        say(partial=f"heard {n} bytes")
    say(final=f"{n} bytes in {loc}.")
    say(done=True)
''')


class Dictation(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        fake = Path(self.dir.name, "fake.py")
        fake.write_text(FAKE, encoding="utf-8")
        self.old = dictation.HELPER
        dictation.HELPER = [sys.executable, str(fake)]
        self.addCleanup(setattr, dictation, "HELPER", self.old)

    def wait_for(self, did, key):
        for _ in range(200):
            v = dictation._get(did).view()
            if v[key]:
                return v
            time.sleep(0.01)
        self.fail(f"no {key}")

    def test_the_page_sends_audio_and_gets_the_text_so_far_then_the_final_text(self):
        self.assertEqual(dictation.engine(), "mac")
        d = server.OPS["dictate_start"](None, {"lang": "de-DE"}, None)
        pcm = base64.b64encode(b"\0\1" * 1600).decode()  # 100 ms
        server.OPS["dictate_audio"](None, {"id": d["id"], "pcm": pcm}, None)
        self.assertEqual(self.wait_for(d["id"], "partial")["partial"], "heard 3200 bytes")
        v = server.OPS["dictate_audio"](None, {"id": d["id"], "pcm": pcm}, None)
        self.assertEqual(set(v), {"id", "text", "partial", "note", "error", "done"})
        end = server.OPS["dictate_stop"](None, {"id": d["id"]}, None)
        self.assertEqual((end["text"], end["partial"], end["done"], end["error"]), ("6400 bytes in de-DE.", "", True, None))
        with self.assertRaisesRegex(RiverError, "this dictation ended"):
            server.OPS["dictate_audio"](None, {"id": d["id"], "pcm": pcm}, None)

    def test_an_error_of_the_helper_reaches_the_page(self):
        d = dictation.start("xx-YY")
        end = dictation.stop(d["id"])
        self.assertEqual((end["error"], end["done"]), ("macOS has no speech model for the language xx-YY", True))

    def test_a_dictation_whose_page_went_away_ends(self):
        d = dictation.start("en-US")
        p = dictation._get(d["id"]).proc
        with mock.patch.object(dictation, "IDLE", -1):
            dictation._sweep()
        p.wait(5)
        with self.assertRaises(RiverError):
            dictation.stop(d["id"])

    def test_the_actions_touch_no_queue(self):
        """Audio comes a few times a second: the action answers without opening the queue."""
        import io
        import json
        h = server.Handler.__new__(server.Handler)
        d = dictation.start("en-US")
        self.addCleanup(lambda: dictation._running.pop(d["id"], None) and None)
        data = json.dumps({"op": "dictate_audio", "args": {"id": d["id"], "pcm": ""}}).encode()
        h.path, h.client_address, h.rfile, out = "/api/action", ("127.0.0.1", 5555), io.BytesIO(data), {}
        h.headers = {"Host": "127.0.0.1:8765", "Content-Length": str(len(data))}
        h._send = lambda code, body, ctype=None: out.update(code=code, body=body)
        with mock.patch.object(core, "connect", side_effect=AssertionError("the queue was opened")):
            h.do_POST()
        self.assertEqual((out["code"], out["body"]["result"]["id"]), (200, d["id"]))
        dictation.stop(d["id"])

    def test_where_the_helper_cannot_run_the_page_uses_the_browser(self):
        dictation.HELPER = None
        with mock.patch.object(core, "PLATFORM", "linux"):
            self.assertIsNone(dictation.engine())
            with self.assertRaisesRegex(RiverError, "needs macOS 26"):
                dictation.start("en-US")
        with mock.patch.object(core, "PLATFORM", "darwin"), mock.patch("platform.mac_ver", return_value=("15.6", "", "")):
            self.assertIsNone(dictation.engine())
        # macOS 26 with a helper in the release (river/bin), or one built here from the source.
        with mock.patch.object(core, "PLATFORM", "darwin"), mock.patch("platform.mac_ver", return_value=("26.1", "", "")):
            built = Path(self.dir.name, "bin")
            with mock.patch.object(dictation, "BUILT", built), mock.patch.object(dictation, "BUNDLED", Path(self.dir.name, "none")):
                self.assertIsNone(dictation.helper())  # BUILD is off in the tests: nothing builds
                built.mkdir()
                dictation._built_path().write_text("", encoding="utf-8")
                self.assertEqual(dictation.helper(), [str(dictation._built_path())])
            with mock.patch.object(dictation, "BUNDLED", Path(self.dir.name, "fake.py")):
                self.assertEqual(dictation.engine(), "mac")


@unittest.skipUnless(os.environ.get("MAXPM_DICTATE_REAL"), "set MAXPM_DICTATE_REAL=1 on a Mac with macOS 26 and Xcode")
class RealHelper(unittest.TestCase):
    """The Swift helper itself, with speech that macOS says (say): built from the source, then fed at real speed."""

    def test_speech_becomes_text(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        exe = os.path.join(d, "maxpm-dictate")
        subprocess.run(["swiftc", "-O", str(dictation.SOURCE), "-o", exe], check=True, capture_output=True)
        wav = os.path.join(d, "s.wav")
        subprocess.run(["say", "-o", wav, "--data-format=LEI16@16000", "Fix the header, then push it."], check=True)
        import wave
        with wave.open(wav) as w:
            pcm = w.readframes(w.getnframes())
        old, dictation.HELPER = dictation.HELPER, [exe]
        self.addCleanup(setattr, dictation, "HELPER", old)
        s = dictation.start("en-US")
        for i in range(0, len(pcm), 3200):
            dictation.audio(s["id"], base64.b64encode(pcm[i:i + 3200]).decode())
            time.sleep(0.1)  # timing: the audio comes at the speed of speech; a slow machine sends it slower
        end = dictation.stop(s["id"])
        self.assertIsNone(end["error"], end)
        self.assertIn("header", end["text"].lower(), end)


if __name__ == "__main__":
    unittest.main()
