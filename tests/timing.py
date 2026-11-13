"""Time limits for tests that wait on another thread or process (#5705).

GitHub's macOS runners are many times slower than a developer's Mac: a test that slept a fixed time and then
checked an event, or that bounded how long a step took, passed here and failed there. A test that waits for an
event uses wait_for; a time bound uses limit(), which scales on CI. tests/test_timing.py refuses a fixed sleep or
a time bound that goes around them (a line can say why it is safe with a '# timing: <why>' comment).
"""
import os
import time


def _scale():
    raw = os.environ.get("MAXPM_TEST_TIME_SCALE")
    if raw:
        return max(1.0, float(raw))
    return 5.0 if os.environ.get("CI") else 1.0


SCALE = _scale()


def limit(seconds, below=None):
    """A time bound for this machine: the seconds that a fast machine needs, times SCALE on CI. below: the time
    that the defect the bound catches takes (a timeout, a fake's wait); the bound stays under it, so the test
    still fails on that defect."""
    out = seconds * SCALE
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
