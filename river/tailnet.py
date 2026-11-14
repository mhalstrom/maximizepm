"""The phone reaches MaximizePM over Tailscale (#5863, #5853; mark 2026-11-14: 'a process that sets it up via tail
scale and SSH is perfectly fine'). The person runs `tailscale serve --bg <serve_port>`: Tailscale then answers
https://<this computer>.<tailnet>.ts.net/ on the person's tailnet only, and forwards each request to maxpm serve on
127.0.0.1 with the header Tailscale-User-Login (the login of the person who made it). Tailscale removes that header
when a client sends it, and adds none to a Funnel (public) request or to a request from a tagged device.

maxpm serve has no sign-in, so mcp.local_refusal passes such a request only when its host is this computer's tailnet
name and its login is one the person allowed (the setting tailnet_logins; empty: the login that owns this computer
in the tailnet). This module reads `tailscale status --json` (cached: no call per request), and makes the QR code of
the address for the setup guide and maxpm phone (a byte-mode QR encoder, so the page loads no code from elsewhere).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

# The Tailscale CLI: on the PATH, or inside the Mac app.
APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
RUNNER = None  # tests: a function(argv) -> (returncode, stdout)
SPAWN = None  # tests: a function(argv, out) -> a Popen-like (poll, wait, terminate) that writes its output to out
LINK_WAIT_S = 15  # seconds that serve_on waits for tailscale serve to end or to print the link of its step
_LINK = re.compile(r"https://\S+")
_PENDING = {"proc": None, "out": None}  # the tailscale serve that waits for the person's approval
CACHE_S = 60  # seconds that a read of tailscale status stays good
_SELF = {"at": 0.0, "value": None}


def cli():
    """The path of the Tailscale CLI, or None."""
    return shutil.which("tailscale") or (APP_CLI if Path(APP_CLI).is_file() else None)


def _run(args, timeout=5):
    if RUNNER is not None:
        return RUNNER(args)
    exe = cli()
    if not exe:
        return 127, ""
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return p.returncode, p.stdout


def read_self():
    """{name, login, running, https} of this computer in its tailnet, now: name is its MagicDNS name (no dot at the
    end), login the login of the user who owns it, https whether the tailnet gives it HTTPS certificates (CertDomains;
    with none, tailscale serve waits with no word, #5865). None when the CLI is missing or says nothing it can read."""
    code, out = _run(["status", "--json"])
    if code != 0:
        return None
    try:
        d = json.loads(out)
    except ValueError:
        return None
    s = d.get("Self") or {}
    user = (d.get("User") or {}).get(str(s.get("UserID"))) or {}
    return {"name": (s.get("DNSName") or "").rstrip(".").lower() or None, "login": user.get("LoginName"),
            "running": d.get("BackendState") == "Running", "https": bool(d.get("CertDomains"))}


def self_info(fresh=False):
    """read_self, read again at most every CACHE_S seconds (mcp.local_refusal asks it for each forwarded request)."""
    t = time.monotonic()
    if fresh or t - _SELF["at"] > CACHE_S:
        _SELF.update(at=t, value=read_self())
    return _SELF["value"]


def allowed_logins(conn=None):
    """The logins that may use the page over the tailnet: the setting tailnet_logins, else the owner's login."""
    from . import core
    close = conn is None
    conn = conn or core.connect()
    try:
        value = core.setting(conn, "tailnet_logins")
    finally:
        if close:
            conn.close()
    got = {x.strip().lower() for x in value.split(",") if x.strip()}
    if not got:
        me = self_info()
        if me and me.get("login"):
            got = {me["login"].lower()}
    return got


_LOGINS = {"at": 0.0, "value": set()}


def login_ok(login):
    """Whether a login that Tailscale gave (Tailscale-User-Login) may use the page; the setting is read again at most
    every CACHE_S seconds."""
    t = time.monotonic()
    if t - _LOGINS["at"] > CACHE_S:
        _LOGINS.update(at=t, value=allowed_logins())
    return bool(login) and login.strip().lower() in _LOGINS["value"]


def forget():
    """Read the status and the setting again at the next request (after maxpm phone, the setup step, or a test)."""
    _SELF.update(at=0.0, value=None)
    _LOGINS.update(at=0.0, value=set())


def serving(port):
    """Whether tailscale serve forwards to 127.0.0.1:<port> (or localhost:<port>) now; None when it cannot tell."""
    code, out = _run(["serve", "status", "--json"])
    if code != 0:
        return None
    return f"127.0.0.1:{port}" in out or f"localhost:{port}" in out


def serve_command(port):
    return f"tailscale serve --bg {port}"


DNS_ADMIN = "https://login.tailscale.com/admin/dns"
NO_HTTPS = (f"HTTPS certificates are off for your tailnet. Open {DNS_ADMIN}, go to HTTPS Certificates and press "
            "Enable; then turn the page on again")


def status(conn):
    """What the setup step and maxpm phone show: installed, running, the name, whether the tailnet gives HTTPS
    certificates, whether it serves the page, the command that turns it on, the address, and its QR code (SVG)."""
    from . import core
    port = core.serve_port_now(conn)
    me = self_info(fresh=True) if cli() or RUNNER else None
    on = serving(port) if me and me.get("running") else None
    url = f"https://{me['name']}/" if me and me.get("name") else None
    step = enable_step()  # read also when it serves: a tailscale serve that ended leaves _PENDING
    return {"installed": bool(cli() or RUNNER), "running": bool(me and me.get("running")), "name": me and me.get("name"),
            "https": bool(on or (me and me.get("https"))), "https_admin": DNS_ADMIN,
            "serving": bool(on), "command": serve_command(port), "url": url,
            "qr": qr_svg(url) if url else None, "logins": sorted(allowed_logins(conn)),
            "enable": None if on else step}


class _Done:
    """A tailscale serve that already ended (tests through RUNNER): its exit code, its output in the file."""

    def __init__(self, code):
        self.returncode = code

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass


def _spawn(args, out):
    """Start the Tailscale CLI with its output in the file out. It runs in a session of its own and writes to a file,
    not a pipe: when Serve is not on for the tailnet, it waits until the person turns it on and only then sets the
    serve setting, also after maxpm phone or a restart of maxpm serve ended (#5865)."""
    if SPAWN is not None:
        return SPAWN(args, out)
    if RUNNER is not None:
        code, text = RUNNER(args)
        Path(out).write_text(text)
        return _Done(code)
    with open(out, "w") as f:
        return subprocess.Popen([cli(), *args], stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)


def _read(out):
    try:
        return Path(out).read_text(errors="replace")
    except OSError:
        return ""


def _said(text):
    """What tailscale serve printed, in one line, with no link and no blank lines."""
    return " ".join(x.strip() for x in text.splitlines() if x.strip() and not _LINK.fullmatch(x.strip()))


def enable_step():
    """{url, text} while a tailscale serve that this process started waits for the person to turn on Serve (or
    HTTPS) for the tailnet at a login.tailscale.com link (the last link it printed is the step now); else None."""
    p = _PENDING["proc"]
    if p is None or p.poll() is not None:
        _PENDING.update(proc=None)
        return None
    text = _read(_PENDING["out"])
    links = _LINK.findall(text)
    return {"url": links[-1] if links else None, "text": _said(text)}


def _failed(conn, code, text):
    from . import core
    return core.RiverError(f"tailscale serve did not start (exit {code}): {_said(text) or 'it said nothing'}; "
                           f"run it in a terminal: {serve_command(core.serve_port_now(conn))}")


def serve_on(conn, actor=None):
    """The person's click in the setup guide: tailscale serve --bg <serve_port>. It changes only this computer's
    Tailscale serve setting, and `tailscale serve --bg <port> off` turns it off. When Serve or HTTPS is not on for
    the tailnet, tailscale serve prints a login.tailscale.com link and waits for the person there: the result then
    holds that step (enable), and the command goes on in the background until the person approves it (#5865)."""
    from . import core
    port = core.serve_port_now(conn)
    if not (cli() or RUNNER or SPAWN):
        raise core.RiverError("Tailscale is not on this computer: install it (tailscale.com/download) and sign in, "
                              "then run maxpm phone again")
    me = self_info(fresh=True)
    if me and me.get("running") and not me.get("https") and not serving(port):
        raise core.RiverError(NO_HTTPS)  # tailscale serve would wait with no word (seen 2026-11-14, #5865)
    if enable_step() is None:  # one tailscale serve at a time: a second click shows the same step
        out = str(core.db_path().with_name("tailscale-serve.out"))
        _PENDING.update(proc=_spawn(["serve", "--bg", str(port)], out), out=out)
    p, out = _PENDING["proc"], _PENDING["out"]
    end = time.monotonic() + LINK_WAIT_S
    while p.poll() is None and not _LINK.search(_read(out)) and time.monotonic() < end:
        time.sleep(0.1)
    forget()
    code, text = p.poll(), _read(out)
    if code is None and not _LINK.search(text):
        p.terminate()
        _PENDING.update(proc=None)
        raise core.RiverError(f"tailscale serve still ran after {LINK_WAIT_S} seconds and gave no link"
                              f"{' (it said: ' + _said(text) + ')' if _said(text) else ''}; run it in a terminal to "
                              f"see what it waits for: {serve_command(port)}")
    if code is not None:
        _PENDING.update(proc=None)
        if code != 0:
            raise _failed(conn, code, text)
    return status(conn)


def wait_enabled(conn, timeout):
    """maxpm phone --on in a terminal: wait until the person approved the step and tailscale serve ended, at most
    timeout seconds; then the status (with enable when it still waits)."""
    p = _PENDING["proc"]
    if p is not None:
        try:
            code = p.wait(timeout)
        except subprocess.TimeoutExpired:
            code = None
        if code is not None:
            text = _read(_PENDING["out"])
            _PENDING.update(proc=None)
            forget()
            if code != 0:
                raise _failed(conn, code, text)
    return status(conn)


# ---- QR code (ISO/IEC 18004), byte mode, error correction level M, versions 1-10 -------------------------------
# After Nayuki's QR Code generator (MIT): the same steps, cut down to one mode and one level.

_ECC_M = [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26]  # error correction codewords per block, version 1-10
_BLOCKS_M = [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5]          # error correction blocks, version 1-10


def _raw_modules(ver):
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver):
    return _raw_modules(ver) // 8 - _ECC_M[ver] * _BLOCKS_M[ver]


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _codewords(data):
    """The data codewords with the error correction, interleaved, for the smallest version that holds data."""
    for ver in range(1, 11):
        bits_count = 8 if ver < 10 else 16
        if 4 + bits_count + 8 * len(data) <= _data_codewords(ver) * 8:
            break
    else:
        raise ValueError("the text is too long for a QR code here (at most about 200 bytes)")
    bits = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in reversed(range(n)))
    put(0b0100, 4)
    put(len(data), bits_count)
    for b in data:
        put(b, 8)
    cap = _data_codewords(ver) * 8
    put(0, min(4, cap - len(bits)))
    put(0, (8 - len(bits) % 8) % 8)
    pad = 0xEC
    while len(bits) < cap:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    nblocks, ecclen, raw = _BLOCKS_M[ver], _ECC_M[ver], _raw_modules(ver) // 8
    nshort, shortlen = nblocks - raw % nblocks, raw // nblocks
    div, blocks, k = _rs_divisor(ecclen), [], 0
    for i in range(nblocks):
        dat = words[k:k + shortlen - ecclen + (0 if i < nshort else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, div)
        if i < nshort:
            dat.append(0)
        blocks.append(dat + ecc)
    out = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != shortlen - ecclen or j >= nshort:
                out.append(blk[i])
    return ver, out


def qr_matrix(text):
    """The modules of a QR code of text (UTF-8): a list of rows of bools, True is dark; no quiet zone."""
    ver, data = _codewords(text.encode("utf-8"))
    size = ver * 4 + 17
    mod = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setf(x, y, dark):
        mod[y][x] = dark
        fn[y][x] = True
    for i in range(size):
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    setf(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    if ver > 1:
        n = ver // 7 + 2
        step = (ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
        pos = list(reversed([size - 7 - i * step for i in range(n - 1)] + [6]))
        for i in range(n):
            for j in range(n):
                if (i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        setf(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)

    def format_bits(mask):
        d = (0 << 3) | mask  # level M is 00
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        return ((d << 10) | rem) ^ 0x5412

    def draw_format(mask):
        b = format_bits(mask)
        bit = lambda i: (b >> i) & 1 == 1  # noqa: E731
        for i in range(6):
            setf(8, i, bit(i))
        setf(8, 7, bit(6))
        setf(8, 8, bit(7))
        setf(7, 8, bit(8))
        for i in range(9, 15):
            setf(14 - i, 8, bit(i))
        for i in range(8):
            setf(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            setf(8, size - 15 + i, bit(i))
        setf(8, size - 8, True)
    draw_format(0)
    if ver >= 7:
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        b = (ver << 12) | rem
        for i in range(18):
            dark = (b >> i) & 1 == 1
            a, c = size - 11 + i % 3, i // 3
            setf(a, c, dark)
            setf(c, a, dark)
    i, right = 0, size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not fn[y][x] and i < len(data) * 8:
                    mod[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2
    masks = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]

    def apply(m):
        for y in range(size):
            for x in range(size):
                if not fn[y][x] and masks[m](x, y):
                    mod[y][x] = not mod[y][x]
    best, best_score = 0, None
    for m in range(8):
        apply(m)
        draw_format(m)
        s = _penalty(mod)
        if best_score is None or s < best_score:
            best, best_score = m, s
        apply(m)  # undo: the mask is its own inverse
    apply(best)
    draw_format(best)
    return mod


def _penalty(mod):
    """A mask's score (lower is better): runs of five or more of one color, 2x2 blocks of one color, and the
    balance of dark and light (rules 1, 2 and 4 of the standard; rule 3, the finder-like patterns, is left out)."""
    size, score = len(mod), 0
    for lines in (mod, [list(c) for c in zip(*mod)]):
        for row in lines:
            run, prev = 0, None
            for v in row + [None]:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, v
    for y in range(size - 1):
        for x in range(size - 1):
            if mod[y][x] == mod[y][x + 1] == mod[y + 1][x] == mod[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, mod))
    total = size * size
    score += (abs(dark * 20 - total * 10) + total - 1) // total * 10 - 10 if total else 0
    return score


def qr_svg(text, scale=6):
    """The QR code of text as an SVG (dark modules on white, a quiet zone of four modules)."""
    from html import escape
    m = qr_matrix(text)
    n = len(m) + 8
    label = escape(text, quote=True)
    path = "".join(f"M{x + 4} {y + 4}h1v1h-1z" for y, row in enumerate(m) for x, v in enumerate(row) if v)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{n * scale}" height="{n * scale}" '
            f'shape-rendering="crispEdges" role="img" aria-label="QR code of {label}"><rect width="{n}" height="{n}" '
            f'fill="#fff"/><path d="{path}" fill="#000"/></svg>')


def qr_text(text):
    """The QR code of text for a terminal: two rows of modules in each line, with block characters (dark on light)."""
    m = qr_matrix(text)
    n = len(m)
    pad = [[False] * (n + 4) for _ in range(2)]
    rows = pad + [[False, False] + r + [False, False] for r in m] + pad + [[False] * (n + 4)]
    out = []
    for y in range(0, len(rows) - 1, 2):
        top, bot = rows[y], rows[y + 1]
        # Light modules print as full blocks, so the code shows dark on light on a dark terminal too.
        out.append("".join(" " if t and b else "▀" if not t and b else "▄" if t and not b else "█"
                           for t, b in zip(top, bot)))
    return "\n".join(out)
