"""Time limits for tests that wait on another thread or process (#5705).

GitHub's macOS runners are many times slower than a developer's Mac: a test that slept a fixed time and then
checked an event, or that bounded how long a step took, passed here and failed there. A test that waits for an
event uses wait_for; a time bound uses limit(), which scales on CI. tests/test_timing.py refuses a fixed sleep or
a time bound that goes around them (a line can say why it is safe with a '# timing: <why>' comment).
"""
import os
import time
import unittest


def skip_local_only():
    """True on GitHub Actions (MAXPM_SKIP_LOCAL_ONLY=1, or GITHUB_ACTIONS=true with it unset; #6504).
    Tests marked with local_only() still run on a developer's machine."""
    raw = os.environ.get("MAXPM_SKIP_LOCAL_ONLY")
    if raw is not None:
        return raw.strip().lower() not in ("", "0", "false", "no")
    return bool(os.environ.get("GITHUB_ACTIONS"))


def local_only(reason):
    """Skip a test on GitHub Actions; still runs locally (#6504). For tests that depend on wall-clock latency,
    load, thread timing, live child processes, real Chrome, or DNS."""
    return unittest.skipIf(skip_local_only(), f"local only ({reason})")


def _scale():
    raw = os.environ.get("MAXPM_TEST_TIME_SCALE")
    if raw:
        return max(1.0, float(raw))
    return 5.0 if os.environ.get("CI") else 1.0


SCALE = _scale()
# #6218: a step that waits on a real Chrome (a click and the page read before and after it) is slower on CI than SCALE
# says: Chrome is a second process on the runner's few cores. The 'no change' click took 0.79 s on the macOS runner
# of release v1.4.0 against 0.1 x 5. On a developer's Mac the bound stays the seconds given (policy p147).
CHROME = 3.0 if SCALE > 1 else 1.0


def limit(seconds, below=None, chrome=False):
    """A time bound for this machine: the seconds that a fast machine needs, times SCALE on CI. below: the time
    that the defect the bound catches takes (a timeout, a fake's wait); the bound stays under it, so the test
    still fails on that defect. chrome: the step waits on a real Chrome; on CI the bound is CHROME times more."""
    out = seconds * SCALE * (CHROME if chrome else 1.0)
    if below is not None:
        out = min(out, max(seconds, below * 0.8))
    return out


def wait_for(check, timeout=10, what="the event", step=0.02):
    """Call check() until it gives a true value, and return that value. Fail after limit(timeout) seconds."""
    end = time.monotonic() + limit(timeout)
    while True:
        got = check()
        if got:
            return got
        if time.monotonic() >= end:
            raise AssertionError(f"{what} did not happen within {limit(timeout):g}s")
        time.sleep(step)
