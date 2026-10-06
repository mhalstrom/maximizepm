"""Notification dispatcher: sends the needs-you outbox through channel adapters.

A channel adapter is a function make(conn) -> send(title, body, url); send
raises on failure. Register one with register_channel(name, make). The
dispatcher groups pending rows per channel, waits notify_batch_window after
the oldest one so events close together go out as one message, then marks
every row sent or failed (failed rows retry, see core.NOTIFY_MAX_ATTEMPTS).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import core
from .core import RiverError

ADAPTERS = {}
SERVE_PORT = {"port": None}  # set by maxpm serve, so links point at the page that is running


def register_channel(name, make):
    ADAPTERS[name] = make


def _log_channel(conn):
    """Writes one JSON line per message next to the database. For tests and for checking the setup."""
    path = Path(core.db_path()).with_name("notifications.log")

    def send(title, body, url):
        with path.open("a") as f:
            f.write(json.dumps({"at": core.iso(core.now()), "title": title, "body": body, "url": url}) + "\n")
    return send


register_channel("log", _log_channel)


def _header(text):
    """HTTP headers are Latin-1; ntfy reads RFC 2047 for anything else."""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        import base64
        return "=?UTF-8?B?" + base64.b64encode(text.encode()).decode() + "?="


def _ntfy_channel(conn):
    """POST to <ntfy_url>/<ntfy_topic>. The topic is the secret: keep it out of logs and repositories."""
    import urllib.request
    base = core.setting(conn, "ntfy_url").rstrip("/")
    topic = core.setting(conn, "ntfy_topic")
    token = core.setting(conn, "ntfy_token")
    if not topic:
        raise RiverError("ntfy has no topic; run: maxpm notify setup ntfy")

    def send(title, body, url):
        headers = {"Title": _header(title), "Tags": "bell", "Content-Type": "text/plain; charset=utf-8",
                   "Priority": core.setting(conn, "ntfy_priority")}
        if not url or _is_local(url):
            url = core.setting(conn, "ntfy_click")
        if url:
            headers["Click"] = url
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"{base}/{topic}", data=body.encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status >= 300:
                raise OSError(f"ntfy answered {r.status}")
    return send


register_channel("ntfy", _ntfy_channel)


PASSWORD_FILE = Path("~/.config/maxpm/smtp-password")


def smtp_password():
    """MAXPM_SMTP_PASSWORD, or a file only its owner can read. Never the settings table."""
    import os
    import stat
    if os.environ.get("MAXPM_SMTP_PASSWORD"):
        return os.environ["MAXPM_SMTP_PASSWORD"]
    f = PASSWORD_FILE.expanduser()
    if not f.exists():
        return None
    # Windows has no POSIX modes (chmod does nothing there); the file sits in the user's own profile folder.
    if os.name != "nt" and f.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RiverError(f"refused: {f} can be read by other users; run: chmod 600 {f}")
    return f.read_text().strip()


def _email_channel(conn):
    import smtplib
    from email.message import EmailMessage
    to = core.setting(conn, "email_to")
    host = core.setting(conn, "smtp_host")
    port = int(core.setting(conn, "smtp_port"))
    user = core.setting(conn, "smtp_user")
    sender = core.setting(conn, "email_from") or user
    missing = [k for k, v in (("email_to", to), ("smtp_host", host), ("email_from", sender)) if not v]
    if missing:
        raise RiverError(f"email needs {', '.join(missing)}: maxpm config set <key> <value>")
    password = smtp_password()
    if user and not password:
        raise RiverError(f"email: smtp_user is set but there is no password; set MAXPM_SMTP_PASSWORD "
                         f"or write it to {PASSWORD_FILE} (chmod 600)")

    def send(title, body, url):
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, sender, to
        msg.set_content(body + (f"\n\nOpen: {url}\n" if url else "\n"))
        smtp = smtplib.SMTP_SSL(host, port, timeout=20) if port == 465 else smtplib.SMTP(host, port, timeout=20)
        with smtp:
            if port != 465:
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    return send


register_channel("email", _email_channel)


def _mac_channel(conn):
    """A macOS banner. terminal-notifier (if on PATH) opens the page on click; osascript is the
    fallback, and a click on its banner opens Script Editor, so the page link goes in the subtitle."""
    import shutil
    import subprocess
    import sys
    if sys.platform != "darwin":
        raise RiverError("the mac channel needs macOS; remove mac from notify_channels on this machine")
    notifier = shutil.which("terminal-notifier")

    def send(title, body, url):
        if notifier:
            cmd = [notifier, "-title", title, "-message", body, "-group", "maxpm"]
            if url:
                cmd += ["-open", url]
        else:
            # The texts go in as arguments, never into the script, so quotes in a title are harmless.
            cmd = ["osascript", "-e", "on run argv",
                   "-e", "display notification (item 2 of argv) with title (item 1 of argv) subtitle (item 3 of argv)",
                   "-e", "end run", title, body, url or ""]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise OSError(f"{Path(cmd[0]).name} exited {r.returncode}: {r.stderr.strip()[:200]}")
    return send


register_channel("mac", _mac_channel)


def setup_ntfy(conn, url=None, token=None, actor=None):
    """Make a random topic, store it, add ntfy to notify_channels, and say how to subscribe on the phone."""
    import secrets
    topic = "maxpm-" + secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:22]
    if url:
        core.config_set(conn, "ntfy_url", url, actor=actor)
    if token:
        core.config_set(conn, "ntfy_token", token, actor=actor)
    core.config_set(conn, "ntfy_topic", topic, actor=actor)
    chans = core._channels(core.setting(conn, "notify_channels"))
    if "ntfy" not in chans:
        core.config_set(conn, "notify_channels", ",".join(chans + ["ntfy"]), actor=actor)
    server = core.setting(conn, "ntfy_url")
    return {"subscribe": [
        "ntfy is set up. On the phone:",
        "  1. Install the ntfy app (App Store or Google Play).",
        f"  2. Subscribe to topic {topic}" + ("" if server == "https://ntfy.sh" else f" on server {server}") + ".",
        "     Keep the topic secret: anyone who knows it can read these notifications.",
        "  3. Test it: maxpm notify test ntfy",
        "     No banner or lock-screen alert? In the phone's settings, allow notifications for the ntfy app",
        "     (banners, lock screen, sound) and exempt it from Focus / Do Not Disturb and battery limits.",
        "     MaximizePM sends at ntfy priority high (maxpm config set ntfy_priority urgent|default|low|min).",
        "MaximizePM shows only the end of the topic from now on (maxpm config get ntfy_topic).",
    ]}


def _is_local(url):
    """A link that only works on this computer, such as the river page."""
    import urllib.parse
    return (urllib.parse.urlparse(url).hostname or "") in ("127.0.0.1", "localhost", "::1")


def page_url(conn, item_id=None, terminal=None):
    """The river page: on an item, or on the Terminal of an agent (the agent's name)."""
    import urllib.parse
    base = f"http://127.0.0.1:{SERVE_PORT['port'] or core.setting(conn, 'serve_port')}/"
    return base + (f"#terminal-{urllib.parse.quote(terminal, safe='')}" if terminal else f"#item-{item_id}" if item_id else "")


def compose(conn, rows):
    """One message for a batch of outbox rows on one channel."""
    if len(rows) == 1:
        r = rows[0]
        # Open the session of the agent that asked or added the item, when it has a web link. An agent that
        # waits on a prompt in its terminal and has no such link: its Terminal on the page.
        url = (core.origin_session_url(conn, r["item_id"], r["message_id"])
               or page_url(conn, r["item_id"], core.prompt_alert_agent(conn, r["message_id"])))
        return "MaximizePM: needs you", r["summary"], url
    lines = [f"- {r['summary']}" for r in rows[:10]]
    if len(rows) > 10:
        lines.append(f"- and {len(rows) - 10} more")
    # One session behind every row: open it; else the page (phones get ntfy_click instead).
    urls = {core.origin_session_url(conn, r["item_id"], r["message_id"]) for r in rows}
    url = urls.pop() if len(urls) == 1 and None not in urls else page_url(conn)
    return f"MaximizePM: {len(rows)} things need you", "\n".join(lines), url


def _adapter(conn, channel):
    make = ADAPTERS.get(channel)
    if not make:
        raise RiverError(f"no adapter for channel {channel!r}; known: {', '.join(sorted(ADAPTERS)) or '(none)'}")
    return make(conn)


def run(conn, now_=False):
    """Send every channel batch that is due. Returns one result per channel batch it tried."""
    with core.tx(conn):
        core.sync_needs_you(conn)
    t = core.now()
    by_channel = {}
    for r in core.outbox(conn):
        by_channel.setdefault(r["channel"], []).append(r)
    results = []
    for channel, rows in sorted(by_channel.items()):
        # A channel may have its own window (email_batch_window); else notify_batch_window.
        key = f"{channel}_batch_window" if f"{channel}_batch_window" in core.DEFAULT_SETTINGS else "notify_batch_window"
        window = core.parse_duration(core.setting(conn, key))
        oldest = min(core.parse_iso(r["created_at"]) for r in rows)
        if not now_ and t - oldest < window:
            results.append({"channel": channel, "rows": len(rows), "sent": False,
                            "waiting": core._short(window - (t - oldest))})
            continue
        title, body, url = compose(conn, rows)
        try:
            _adapter(conn, channel)(title, body, url)
        except Exception as e:  # any adapter failure is recorded and retried, never raised to the caller
            for r in rows:
                core.outbox_mark(conn, r["id"], False, f"{type(e).__name__}: {e}")
            results.append({"channel": channel, "rows": len(rows), "sent": False, "error": str(e)})
        else:
            for r in rows:
                core.outbox_mark(conn, r["id"], True)
            results.append({"channel": channel, "rows": len(rows), "sent": True, "title": title})
    return results


def test(conn, channel):
    """Send a test message on one channel now, outside the outbox."""
    try:
        _adapter(conn, channel)("MaximizePM: test", "This is a test notification from maxpm notify test.",
                                  core.session_url_from_env() or page_url(conn))
    except RiverError:
        raise
    except Exception as e:
        return {"channel": channel, "ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"channel": channel, "ok": True}


def status(conn):
    configured = core._channels(core.setting(conn, "notify_channels"))
    names = sorted(set(configured) | {r[0] for r in conn.execute("SELECT DISTINCT channel FROM notifications")})
    out = []
    for ch in names:
        last_sent = conn.execute("SELECT MAX(sent_at) FROM notifications WHERE channel=? AND state='sent'", (ch,)).fetchone()[0]
        err = conn.execute("SELECT last_error, attempts FROM notifications WHERE channel=? AND state='failed' "
                           "ORDER BY id DESC LIMIT 1", (ch,)).fetchone()
        out.append({
            "channel": ch, "configured": ch in configured, "adapter": ch in ADAPTERS,
            "pending": sum(1 for r in core.outbox(conn, ch)),
            "last_sent": last_sent,
            "last_error": err["last_error"] if err else None,
        })
    return {"channels": out, "interval": core.setting(conn, "notify_interval"),
            "batch_window": core.setting(conn, "notify_batch_window")}


def loop(stop, interval_s=None):
    """Run the dispatcher until stop (a threading.Event) is set. For maxpm serve and maxpm notify run."""
    while not stop.is_set():
        wait = interval_s or 30
        conn = core.connect()
        try:
            wait = interval_s or core.parse_duration(core.setting(conn, "notify_interval")).total_seconds()
            from . import server
            server.watch_prompts(conn)  # an agent that waits on a prompt in its tmux pane: tell the person
            server.watch_busy(conn)  # an agent that is busy with no river command keeps its leases
            server.fresh_sessions(conn)  # work for an agent idle at its prompt goes to a fresh session
            server.auto_release(conn)  # a ready review gets a reviewer, a ready deploy its deployer
            server.auto_context(conn)  # goal_context on: a goal with ready work gets a context session (a base)
            server.auto_tidy(conn)  # every tidy_every: close the tmux panes of sessions that are done
            server.auto_end_orphans(conn)  # and end the tmux servers the tests left behind with no socket
            if core._channels(core.setting(conn, "notify_channels")):
                run(conn)
            if SERVE_PORT.get("port") and server.reload_ready(conn):  # maxpm serve only: run the new code
                conn.close()
                server.restart_now("the MaximizePM code changed")
        except Exception as e:  # keep the loop alive; the next pass retries
            print(f"maxpm notify: {e}", flush=True)
        finally:
            conn.close()
        stop.wait(max(1.0, wait))

