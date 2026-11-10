"""Dictation (#5107): the page records the microphone and sends the audio to maxpm serve; on a Mac with macOS 26, serve
hands it to maxpm-dictate (river/dictate/main.swift), which turns speech into text on the device with Apple's
SpeechAnalyzer. Where that cannot run, the page uses the browser's own speech recognition, and where neither exists
it shows no microphone.

The audio comes through the page's actions (dictate_start, dictate_audio, dictate_stop), so it also comes through the
relay from a phone: 16-bit mono PCM at 16000 Hz, base64, a piece every few hundred milliseconds. Each reply carries
the text so far: the final parts and the part that can still change.

The helper: a prebuilt one in the release (river/bin/maxpm-dictate), else one built here from the source when Xcode's
swiftc is on this Mac (once, in the background, into ~/.maximizepm/bin), else none. It reads audio only from serve,
so it needs no microphone permission; SpeechAnalyzer on the device needs no speech recognition permission either."""
from __future__ import annotations

import base64
import hashlib
import json
import platform
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import core
from .core import RiverError

SOURCE = Path(__file__).resolve().parent / "dictate" / "main.swift"
BUNDLED = Path(__file__).resolve().parent / "bin" / "maxpm-dictate"
BUILT = Path("~/.maximizepm/bin").expanduser()
HELPER = None  # tests: the command line of a fake helper
BUILD = True  # tests: False, so a test never builds the helper into ~/.maximizepm/bin
IDLE = 60  # seconds: a dictation that gets no audio for this long ends (the page closed in the middle)
STOP_WAIT = 8  # seconds: how long dictate_stop waits for the last final text
_build = {"thread": None, "error": None}
_lock = threading.Lock()
_running = {}  # id -> Dictation


def mac_ok():
    """macOS 26 or later: SpeechAnalyzer is there."""
    if core.PLATFORM != "darwin":
        return False
    try:
        return int((platform.mac_ver()[0] or "0").split(".")[0]) >= 26
    except ValueError:
        return False


def _built_path():
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:12]
    return BUILT / f"maxpm-dictate-{digest}"


def helper(build=True):
    """The helper's command line, or None. With build, a Mac with swiftc and no helper starts to build one; the page
    uses the browser's speech recognition until it is there."""
    if HELPER:
        return list(HELPER)
    if not mac_ok():
        return None
    if BUNDLED.is_file():
        return [str(BUNDLED)]
    if not SOURCE.is_file():
        return None
    out = _built_path()
    if out.is_file():
        return [str(out)]
    if build and BUILD:
        _start_build(out)
    return None


def _start_build(out):
    with _lock:
        if _build["thread"] is not None or _build["error"]:
            return
        swiftc = shutil.which("swiftc") or ("/usr/bin/swiftc" if Path("/usr/bin/swiftc").exists() else None)
        if not swiftc:
            _build["error"] = "no swiftc (Xcode) on this Mac"
            return

        def run():
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp")
            try:
                r = subprocess.run([swiftc, "-O", str(SOURCE), "-o", str(tmp)], capture_output=True, text=True,
                                   timeout=600)
                if r.returncode:
                    _build["error"] = (r.stderr or r.stdout).strip()[-400:]
                    return
                tmp.replace(out)
            except (OSError, subprocess.SubprocessError) as e:
                _build["error"] = str(e)
            finally:
                _build["thread"] = None
        _build["thread"] = threading.Thread(target=run, daemon=True, name="maxpm-dictate build")
        _build["thread"].start()


def engine():
    """What the page records with: "mac" (the helper on this computer), or None (the browser's own recognition)."""
    return "mac" if helper() else None


class Dictation:
    """One helper process for one dictation, and the text it gave so far."""

    def __init__(self, cmd, lang):
        self.id = secrets.token_hex(8)
        self.finals, self.partial, self.note, self.error, self.done = [], "", "", None, False
        self.seen = time.monotonic()
        self.cond = threading.Condition()
        try:
            self.proc = subprocess.Popen([*cmd, "--locale", lang], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, bufsize=0)
        except OSError as e:
            raise RiverError(f"dictation did not start: {e}")
        threading.Thread(target=self._read, daemon=True, name=f"dictate {self.id}").start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            with self.cond:
                if "partial" in m:
                    self.partial = m["partial"]
                elif "final" in m:
                    self.finals.append(m["final"].strip())
                    self.partial = ""
                elif "installing" in m:
                    self.note = (f"macOS installs the speech model of {m['installing']} (once); the words come when "
                                 f"it is there")
                elif "ready" in m:
                    self.note = ""
                elif "error" in m:
                    self.error = m["error"]
                elif m.get("done"):
                    self.done = True
                self.cond.notify_all()
        self.proc.wait()
        with self.cond:
            if not self.done and self.error is None and self.proc.returncode:
                self.error = f"the dictation helper ended (exit {self.proc.returncode})"
            self.done = True
            self.cond.notify_all()

    def view(self):
        return {"id": self.id, "text": " ".join(x for x in self.finals if x), "partial": self.partial,
                "note": self.note, "error": self.error, "done": self.done}

    def write(self, pcm):
        self.seen = time.monotonic()
        try:
            self.proc.stdin.write(pcm)
        except (BrokenPipeError, ValueError, OSError):
            pass  # the helper ended: its error is in view()

    def finish(self, wait=STOP_WAIT):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        end = time.monotonic() + wait
        with self.cond:
            while not self.done and time.monotonic() < end:
                self.cond.wait(end - time.monotonic())
        if not self.done:
            self.proc.kill()
        return self.view()


def _sweep():
    """End the dictations whose page went away (no audio for IDLE seconds)."""
    now = time.monotonic()
    with _lock:
        old = [d for d in _running.values() if now - d.seen > IDLE]
        for d in old:
            _running.pop(d.id, None)
    for d in old:
        d.proc.kill()


def _get(did):
    with _lock:
        d = _running.get(str(did))
    if d is None:
        raise RiverError("this dictation ended; click the microphone again")
    return d


def start(lang=None):
    """Start a dictation in the language lang (the browser's, such as en-US)."""
    _sweep()
    cmd = helper()
    if not cmd:
        raise RiverError("dictation on this computer needs macOS 26 and the maxpm-dictate helper; the page uses the "
                         "browser's speech recognition instead")
    d = Dictation(cmd, str(lang or "en-US"))
    with _lock:
        _running[d.id] = d
    return d.view()


def audio(did, pcm_b64):
    """A piece of audio: 16-bit mono PCM at 16000 Hz, base64. Returns the text so far."""
    d = _get(did)
    d.write(base64.b64decode(pcm_b64 or ""))
    return d.view()


def stop(did):
    """The person stopped: the helper reads the rest, gives the last final text, and ends."""
    d = _get(did)
    with _lock:
        _running.pop(d.id, None)
    return d.finish()
