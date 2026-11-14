"""Sign-ins that fill a session's browser by themselves (#5826, design canvas credential-vault).

A sign-in is a secret of kind 'signin' (vault.signin_save): a site, an account, a password, and (when the person
allows agents to pass a 2-step check) an authenticator key. When a page of a session's browser loads on the sign-in
page of a site that a sign-in names, and a grant covers the session (vault.allowed), maxpm serve fills the account and
the password itself and presses Enter: the agent sends no command and never sees the field. Only on the real site
(https, the host is the site or below it), so a look-alike page gets nothing.

One try (policy g5): a password that the site refuses, a robot check, or a 2-step check that the agents may not pass
ends the fill for that sign-in: one alert to the manager (else the person) with a screenshot, and no fill again until
a person resets it (maxpm secret signin <name> --reset) or saves a new password.

scrub() takes the values that serve filled, and the values of the password fields of the page, out of what maxpm
browser eval, text, html, extract and snapshot reply."""

import base64
import os
import sys
import threading
import time
from urllib.parse import urlsplit

from . import core, vault

SETTLE = 0.6       # seconds after a page load before the fields are looked at: the page's script draws the form
SUBMIT_WAIT = 10.0  # seconds that a submit waits for the next page or for the form to change
POLL = 0.5
STEPS_MAX = 4      # account, password, code, and one more: a sign-in that needs more is not one this fill knows
AGAIN = 600.0      # seconds: the password form again this soon after a submit of the password is a refused password
RUNNER = None      # tests: fn(b) instead of a thread, so check() runs at once

# Marks the visible fields of a sign-in form (data-maxpm-signin = user, pw, otp) and says what the page shows.
FIND_JS = r"""(() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && !e.disabled && !e.readOnly; };
  const words = e => [e.name, e.id, e.getAttribute('autocomplete'), e.getAttribute('aria-label'), e.placeholder]
    .filter(Boolean).join(' ').toLowerCase();
  const type = e => (e.getAttribute('type') || 'text').toLowerCase();
  const all = [...document.querySelectorAll('input')].filter(vis);
  const pw = all.find(e => type(e) === 'password');
  const otp = pw ? null : all.find(e => /one-time-code/.test(words(e)) || (['text', 'tel', 'number'].includes(type(e))
    && /(^|[^a-z])(otp|totp|2fa|mfa|pin|verification|code)([^a-z]|$)/.test(words(e))));
  const user = all.find(e => e !== otp && ['email', 'text', 'tel'].includes(type(e))
    && /user|email|login|account|identifier/.test(words(e)));
  document.querySelectorAll('[data-maxpm-signin]').forEach(e => e.removeAttribute('data-maxpm-signin'));
  if (pw) pw.setAttribute('data-maxpm-signin', 'pw');
  if (otp) otp.setAttribute('data-maxpm-signin', 'otp');
  if (user) user.setAttribute('data-maxpm-signin', 'user');
  const robot = !!document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], '
    + 'iframe[src*="challenges.cloudflare.com"], .g-recaptcha, .h-captcha, .cf-turnstile');
  const said = [...document.querySelectorAll('[role=alert], [aria-live=assertive]')].map(e => e.innerText.trim())
    .filter(Boolean).join(' / ').slice(0, 200);
  return {pw: !!pw, otp: !!otp, user: !!user, user_value: user ? user.value : '', robot, said, url: location.href};
})()"""

FOCUS_JS = ("(() => { const e = document.querySelector('[data-maxpm-signin=%s]'); if (!e) return false; e.focus(); "
            "if (e.select) e.select(); return true; })()")
# The values of the page's password fields, for scrub(): the top document and its same-origin frames.
VALUES_JS = ("(() => { const out = []; const look = d => { try { d.querySelectorAll('input[type=password]').forEach("
             "e => { if (e.value) out.push(e.value); }); d.querySelectorAll('iframe').forEach(f => { if (f.contentDocument) "
             "look(f.contentDocument); }); } catch (x) {} }; look(document); return out; })()")


SITES_AGAIN = 30.0  # seconds that soon() keeps the list of sign-in sites before it reads the queue again
_SITES = {"at": 0.0, "sites": (), "db": None}


def _sites():
    """The sites of all sign-ins, read at most every SITES_AGAIN: a page of another site costs no thread."""
    db = os.environ.get("MAXPM_DB")
    if time.time() - _SITES["at"] > SITES_AGAIN or _SITES["db"] != db:
        conn = core.connect()
        try:
            _SITES["sites"] = tuple(r["site"] for r in conn.execute(
                "SELECT site FROM secrets WHERE kind='signin' AND site IS NOT NULL"))
        finally:
            conn.close()
        _SITES.update(at=time.time(), db=db)
    return _SITES["sites"]


def soon(b, url=None):
    """After a page load or a client route in a session's browser: check() in a thread of its own, when the page is
    on a site that a sign-in names."""
    if not getattr(b, "owner", None):
        return
    host = (urlsplit(url or "").hostname or "").lower().removeprefix("www.")
    if url is not None and not any(host == s or host.endswith("." + s) for s in _sites()):
        return
    if RUNNER:
        RUNNER(b)
        return
    threading.Thread(target=lambda: (time.sleep(SETTLE), check(b)), daemon=True,
                     name=f"browser-signin-{b.project}").start()


def check(b, conn=None):
    """Fill the sign-in form of the shown page when a granted sign-in names its site. One check at a time per
    browser; a failure is said once, never tried again."""
    lock = b.__dict__.setdefault("signin_lock", threading.Lock())
    if not lock.acquire(blocking=False):
        return None
    mine = conn is None
    try:
        conn = conn or core.connect()
        return _steps(conn, b)
    except Exception as e:  # a page that went away, Chrome that ended: the next page load checks again
        print(f"maxpm: browser sign-in check: {e.__class__.__name__}: {e}", file=sys.stderr)
        return None
    finally:
        if mine and conn is not None:
            conn.close()
        lock.release()


def _host(url):
    u = urlsplit(url or "")
    host = (u.hostname or "").lower()
    real = u.scheme == "https" or (u.scheme == "http" and host in ("localhost", "127.0.0.1"))
    return host if real else None


def _fields(b):
    f = b.evaluate(FIND_JS, 5)
    return f if isinstance(f, dict) else None


def _type(b, which, text):
    if not b.evaluate(FOCUS_JS % which, 5):
        return False
    b.page("Input.insertText", {"text": text})
    b.__dict__.setdefault("signin_values", set()).add(text)
    return True


def _enter(b):
    from .browser import key_events
    for p in key_events("Enter"):
        b.page("Input.dispatchKeyEvent", p)


def _person_controls(b):
    c = (getattr(b, "state", None) or {}).get("controller") or {}
    return c.get("kind") == "person"


def _steps(conn, b):
    """Up to STEPS_MAX steps of one sign-in: the account, the password, a 2-step code. Returns what it did, for the
    tests: a list of steps, or the failure."""
    done = []
    tries = b.__dict__.setdefault("signin_tries", {})  # name -> when its password went in
    for _ in range(STEPS_MAX):
        if _person_controls(b):
            return done
        f = _fields(b)
        host = _host(f.get("url")) if f else None
        if not f or not host or not (f["pw"] or f["otp"] or f["user"]):
            return done
        creds = [r for r in vault.signins_for(conn, host) if vault.allowed(conn, r["name"], b.owner)["ok"]]
        if not creds:
            return done
        # The sign-in of the account that the page shows already, else the first one of the most exact site.
        cred = next((r for r in creds if f["user_value"] and r["account"] == f["user_value"].strip()), creds[0])
        if cred["fail_at"]:
            return done
        if f["robot"]:
            return _fail(conn, b, cred, "the page shows a robot check")
        if f["otp"]:
            key = vault.backend().get(conn, cred["name"] + vault.TOTP_SUFFIX)
            if cred["two_step"] != "agents" or not key:
                return _fail(conn, b, cred, "the site asks for a 2-step code, and the person passes that step "
                                            "for this sign-in" if cred["two_step"] != "agents" else
                             "the site asks for a 2-step code, and the sign-in has no authenticator key", person=True)
            if "code" in done:
                return _fail(conn, b, cred, "the site refused the 2-step code")
            _type(b, "otp", vault.totp_code(key))
            what = "code"
        elif f["pw"]:
            if "password" in done or time.time() - tries.get(cred["name"], 0) < AGAIN:
                return _fail(conn, b, cred, f["said"] or "the site showed its password form again after the password")
            password = vault.backend().get(conn, cred["name"])
            if password is None:
                return _fail(conn, b, cred, "the password is gone from the store")
            if f["user"] and not f["user_value"]:
                _type(b, "user", cred["account"])
            _type(b, "pw", password.decode(errors="replace"))
            tries[cred["name"]] = time.time()
            what = "password"
        else:  # the account first, the password on the next page (Google, Microsoft)
            if "account" in done or f["user_value"]:
                return done
            _type(b, "user", cred["account"])
            what = "account"
        _enter(b)
        done.append(what)
        with core.tx(conn):
            conn.execute("UPDATE secrets SET used_at=?, used_by=?, uses=uses+1 WHERE name=?",
                         (core.iso(core.now()), b.owner, cred["name"]))
            vault._log(conn, cred["name"], b.owner, "use", f"browser sign-in: {what} on {host}")
        _wait_change(b, f)
    return done


def _wait_change(b, before):
    """After a submit: wait until the address or the form changes, or SUBMIT_WAIT."""
    end = time.time() + SUBMIT_WAIT
    while time.time() < end:
        time.sleep(POLL)
        try:
            f = _fields(b)
        except core.RiverError:
            continue  # the next page loads
        if not f or f.get("url") != before.get("url") or any(f.get(k) != before.get(k) for k in ("pw", "otp", "user")):
            time.sleep(POLL)  # the next page draws its form
            return


def _fail(conn, b, cred, why, person=False):
    """The one try ended: record it, keep a screenshot, and alert once: the manager (else a person). person: a 2-step
    step that is the person's: no failure, the sign-in fills again later; the person gets one alert per AGAIN."""
    told = b.__dict__.setdefault("signin_told", {})
    if person:
        if time.time() - told.get(cred["name"], 0) < AGAIN:
            return {"waits": why}
        told[cred["name"]] = time.time()
    shot = None
    try:
        data = (b.page("Page.captureScreenshot", {"format": "png"}, timeout=10) or {}).get("data")
        if data:
            folder = vault._queue_file(conn).expanduser().resolve().parent / "signin"
            folder.mkdir(exist_ok=True)
            shot = folder / f"{cred['name']}-{time.strftime('%Y%m%d-%H%M%S')}.png"
            shot.write_bytes(base64.b64decode(data))
    except Exception:
        shot = None
    if not person:
        vault.signin_failed(conn, cred["name"], why)
    held = core._held_item(conn, b.owner)
    humans = [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY last_seen DESC")]
    to = (humans[0] if humans else None) if person else (core.active_manager(conn) or (humans[0] if humans else None))
    body = (f"The browser sign-in {cred['name']} ({cred['account']} on {cred['site']}) for {b.owner} "
            + ("waits for you" if person else "stopped") + f": {why}. "
            + (f"Screenshot: {shot}. " if shot else "")
            + ("Pass the step in the session's browser pane (Sessions tab)." if person else
               f"MaximizePM tries no more. A person checks the account, then: maxpm secret signin {cred['name']} --reset"))
    if to:
        with core.tx(conn):
            core._send(conn, "alert", "maxpm", body, to=to, item_id=held["id"] if held else None)
    return {"failed": why, "screenshot": str(shot) if shot else None, "to": to}


def scrub(b, out):
    """out with every value that serve filled in this browser, and each value of the page's password fields, as
    ***: what eval, text, html, extract and snapshot reply never holds a password."""
    values = set(b.__dict__.get("signin_values") or ())
    try:
        if b.running():
            values |= set(b.evaluate(VALUES_JS, 5) or ())
    except Exception:
        pass
    values = sorted((v for v in values if isinstance(v, str) and len(v) >= vault.MASK_MIN), key=len, reverse=True)
    if not values:
        return out

    def walk(x):
        if isinstance(x, str):
            for v in values:
                x = x.replace(v, "***")
            return x
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [walk(v) for v in x]
        return x
    return walk(out)


def note(conn, owner, host):
    """The line for a session's reply on a sign-in page (browser.account_note): the sign-ins that serve fills for it
    on this site."""
    mine = [r for r in vault.signins_for(conn, host) if owner and vault.allowed(conn, r["name"], owner)["ok"]]
    if not mine:
        return None
    r = mine[0]
    if r["fail_at"]:
        return f"the sign-in {r['name']} for {host} stopped ({r['fail_why']}): a person resets it"
    return (f"MaximizePM signs this browser in as {r['account']} on {r['site']} by itself (sign-in {r['name']}): "
            f"type no password; wait a few seconds, then look again")
