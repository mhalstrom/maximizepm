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
# The loop of this process: when it started, and its last complete pass. core.serve_loop reads it from a file.
LOOP = {"started": None, "last_pass": None}
# The loop runs in a process of its own (river/loop.py, #5569): on; and it ended to ask serve for a restart.
CHILD = {"on": False, "restart": False}


def register_channel(name, make):
    ADAPTERS[name] = make


def _log_channel(conn):
    """Writes one JSON line per message next to the database. For tests and for checking the setup."""
    path = Path(core.db_path()).with_name("notifications.log")

    def send(title, body, url):
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": core.iso(core.now()), "title": title, "body": body, "url": url}) + "\n")
    return send


register_channel("log", _log_channel)


# The MaximizePM icon of a notification (#4705), from the brand files of design/: ntfy fetches it by address (the
# phone app shows it beside the text), and terminal-notifier shows the local file in the banner.
NTFY_ICON = "https://maximizepm.com/apple-touch-icon.png"
MAC_ICON = Path(__file__).resolve().parent / "static" / "brand" / "apple-touch-icon.png"


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
        headers = {"Title": _header(title), "Tags": "bell", "Icon": NTFY_ICON, "Content-Type": "text/plain; charset=utf-8",
                   "Priority": core.setting(conn, "ntfy_priority")}
        if not url or _is_local(url):
            # The page runs on this computer: the phone opens it through the relay, else ntfy_click (#4673).
            url = remote_page(url) or core.setting(conn, "ntfy_click")
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
            cmd = [notifier, "-title", _tn_text(title), "-message", _tn_text(body), "-group", "maxpm"]
            if MAC_ICON.is_file():
                cmd += ["-contentImage", str(MAC_ICON)]  # the MaximizePM icon in the banner (#4705)
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
        hint = None if notifier else mac_hint()
        if hint:
            # stderr: maxpm notify test shows it, maxpm serve logs it, and --json output stays clean
            try:
                print(f"maxpm notify: {hint}", file=sys.stderr, flush=True)
            except Exception:
                pass
    return send


register_channel("mac", _mac_channel)


# Windows PowerShell's own app id: a notice needs the id of an app that Windows knows, and PowerShell 5.1 is on
# every Windows 10 and 11. A click on the notice opens its launch address (the page) in the Windows browser.
WINDOWS_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def toast_command(ps, title, body, url):
    """The powershell.exe command that shows one Windows notice. The notice XML goes in as base64 inside a script
    that goes in as -EncodedCommand (base64 of UTF-16LE), so no shell or PowerShell parser reads the texts."""
    import base64
    from xml.sax.saxutils import escape, quoteattr
    launch = f' activationType="protocol" launch={quoteattr(url)}' if url else ""
    xml = (f"<toast{launch}><visual><binding template=\"ToastGeneric\"><text>{escape(title[:200])}</text>"
           f"<text>{escape(body[:1000])}</text></binding></visual></toast>")
    script = (
        "$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]\n"
        "$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]\n"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        f"$x.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{base64.b64encode(xml.encode()).decode()}')))\n"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{WINDOWS_APP_ID}')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($x))\n")
    return [ps, "-NoProfile", "-NonInteractive", "-EncodedCommand",
            base64.b64encode(script.encode("utf-16-le")).decode()]


def _windows_channel(conn):
    """A Windows notice from MaximizePM in WSL 2, the one way it runs on Windows (#5180): Windows PowerShell shows
    it, and a click opens the page in the Windows browser."""
    if not core.is_wsl():
        raise RiverError("the windows channel needs WSL; remove windows from notify_channels on this machine")
    ps = core.windows_exe("powershell.exe")
    if not ps:
        raise RiverError("the windows channel found no powershell.exe; in WSL, keep appendWindowsPath on in /etc/wsl.conf")

    def send(title, body, url):
        r = core.windows_run(toast_command(ps, title, body, url), timeout=30)
        if r.returncode != 0:
            raise OSError(f"powershell.exe exited {r.returncode}: {(r.stderr or '').strip()[:200]}")
    return send


register_channel("windows", _windows_channel)


# The first characters that NSUserDefaults misreads in a terminal-notifier value: [ ( { < and a double quote
# start a property list, and a - starts the next option, so every batch ("- one\n- two") failed (#5042).
# A single quote is safe, but the escape does no harm; a backslash gets one more, as terminal-notifier
# removes one.
_TN_ESCAPE = ("[", "(", "{", "<", '"', "'", "-", "\\")


def _tn_text(text):
    """terminal-notifier reads its values through NSUserDefaults. A value that starts with one of _TN_ESCAPE
    comes back empty, and terminal-notifier exits 2 ('Could not read the -message value'). A leading backslash
    escapes it, and terminal-notifier removes that backslash (#4674, #5042)."""
    text = text or ""
    return "\\" + text if text[:1] in _TN_ESCAPE else text

MAC_HINT = "On macOS, brew install terminal-notifier makes a click on a notice open the page."


def mac_hint():
    """The terminal-notifier hint, one time per machine: only on macOS without terminal-notifier on PATH.
    A marker file beside the queue database records that it was shown."""
    import shutil
    import sys
    if sys.platform != "darwin" or shutil.which("terminal-notifier"):
        return None
    marker = Path(core.db_path()).with_name("mac-hint-shown")
    if marker.exists():
        return None
    try:
        marker.write_text(core.iso(core.now()) + "\n", encoding="utf-8")
    except OSError:
        return None  # without a marker it would show on every notice; stay quiet instead
    return MAC_HINT


def setup_ntfy(conn, url=None, token=None, actor=None, open_=None):
    """Make a random topic, store it, add ntfy to notify_channels, and say how to subscribe on the phone.
    open_ (maxpm or agent) sets notify_open: for the person who runs it, else for everyone."""
    import secrets
    person = _person(conn, actor)
    if open_:
        core.config_set(conn, "notify_open", open_, agent=person, actor=actor)
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
    ] + open_lines(conn, person)}


def _person(conn, name):
    """name when it is a registered person, else None (a setting for everyone)."""
    r = conn.execute("SELECT 1 FROM agents WHERE name=? AND kind='human'", (name,)).fetchone() if name else None
    return name if r else None


OPEN_TEXT = {"maxpm": "the MaximizePM page", "agent": "the agent's own app (claude.ai/code, chatgpt.com/codex)"}


def phone_note(conn):
    """Where a phone tap on a link to the page goes, and why."""
    from . import relay
    cfg = relay.load()
    if cfg:
        return f"A phone opens the MaximizePM page through the relay ({relay.site_url(cfg)}/)."
    return (f"A phone cannot open the MaximizePM page: it runs only on this computer (127.0.0.1). A tap there opens "
            f"ntfy_click ({core.setting(conn, 'ntfy_click')}) instead; maxpm connect makes the page reachable from the phone.")


def open_lines(conn, person=None):
    """The setup text about where a tap opens (notify_open), and the choice."""
    v = core.setting(conn, "notify_open", agent=person)
    flag = f" --agent {person}" if person else ""
    return [f"A tap on a notification opens {OPEN_TEXT[v]} (notify_open {v}"
            + (f" for {person}" if person else "") + ").",
            f"  The other choice: maxpm config set notify_open {'agent' if v == 'maxpm' else 'maxpm'}{flag}",
            "  " + phone_note(conn)]


def remote_page(url):
    """The address of a link to the page on this computer through the relay (maxpm connect), or None."""
    import urllib.parse
    from . import relay
    cfg = relay.load()
    if not cfg:
        return None
    u = urllib.parse.urlsplit(url or "")
    return relay.site_url(cfg) + "/" + u.path.lstrip("/") + (f"?{u.query}" if u.query else "")


def _is_local(url):
    """A link that only works on this computer, such as the river page."""
    import urllib.parse
    return (urllib.parse.urlparse(url).hostname or "") in ("127.0.0.1", "localhost", "::1")


def page_url(conn, item_id=None, terminal=None, person=None, art=None, browser=False):
    """The MaximizePM page: on an item (/?item=<id>), or on the session of an agent that waits on a prompt or asked
    a question (/sessions/<name>, where the person answers it, #4653), or on one of its artifacts
    (/sessions/<name>/artifacts/<id>, #4689), or on its Browser (/sessions/<name>/browser, a sign-in step that is the
    person's, #5849); ?as=<person> for the person it is for."""
    import urllib.parse
    base = f"http://127.0.0.1:{SERVE_PORT['port'] or core.serve_port_now(conn)}/"
    path = (f"sessions/{urllib.parse.quote(terminal, safe='')}" + (f"/artifacts/{art}" if art else "/browser" if browser
                                                                   else "")) if terminal else ""
    q = urllib.parse.urlencode([(k, v) for k, v in (("item", item_id if not terminal else None), ("as", person)) if v])
    return base + path + (f"?{q}" if q else "")


def _is_ai(conn, name):
    return bool(name and conn.execute("SELECT 1 FROM agents WHERE name=? AND kind='ai'", (name,)).fetchone())


def art_id(target):
    """The page's id of an artifact (static/lib.js artId, cyrb53 in base 36): the same for the same target."""
    m = 0xFFFFFFFF
    h1, h2 = 0xDEADBEEF, 0x41C6CE57
    for ch in str(target):
        c = ord(ch)
        h1 = ((h1 ^ c) * 2654435761) & m
        h2 = ((h2 ^ c) * 1597334677) & m
    h1 = (((h1 ^ (h1 >> 16)) * 2246822507) & m) ^ (((h2 ^ (h2 >> 13)) * 3266489909) & m)
    h2 = (((h2 ^ (h2 >> 16)) * 2246822507) & m) ^ (((h1 ^ (h1 >> 13)) * 3266489909) & m)
    n, out = 4294967296 * (2097151 & h2) + h1, ""
    while True:
        n, d = divmod(n, 36)
        out = "0123456789abcdefghijklmnopqrstuvwxyz"[d] + out
        if not n:
            return out


def _artifact_of(conn, who, text):
    """The first file or local page that text names and that is an artifact of the session of who (one the side
    panel shows), or None (#4689)."""
    from . import server, sessions
    hits = [h["target"] for h in sessions._hits_in(text or "", None, "named") if h["kind"] in ("file", "page")]
    if not hits:
        return None

    def allowed(f):
        try:
            server.local_file(conn, f, read=False)
            return True
        except RiverError:
            return False
    try:
        mine = {a["target"] for a in sessions.artifacts(conn, None, allowed, agent=who)}
    except Exception:  # a transcript that cannot be read: no artifact link
        return None
    return next((t for t in hits if t in mine), None)


def _page_link(conn, r, person):
    """The page at the thing: a file or page of the session that the notice names (#4689), the session of the
    agent that asked or waits on a prompt, else the item."""
    who = core.origin_agent(conn, r["item_id"], r["message_id"])
    if _is_ai(conn, who):
        art = _artifact_of(conn, who, r.get("summary"))
        if art:
            return page_url(conn, terminal=who, person=person, art=art_id(art))
    if r["message_id"] is not None and _is_ai(conn, who):
        return page_url(conn, terminal=who, person=person)
    return page_url(conn, r["item_id"], person=person)


def open_for(conn, rows):
    """notify_open for a batch, and its person: a person's own value over the global one (#4673)."""
    people = {r.get("human") for r in rows}
    person = people.pop() if len(people) == 1 else None
    item = rows[0]["item_id"] if len(rows) == 1 else None
    return core.setting(conn, "notify_open", item_id=item, agent=person), person


def _title(conn, r):
    """The notice's title: what the session that sent it works on (#4695), else "needs you"."""
    from . import sessions
    who = core.origin_agent(conn, None, r["message_id"]) if r["message_id"] is not None else None
    if not _is_ai(conn, who):
        return "MaximizePM: needs you"
    try:
        return "MaximizePM: " + sessions.work_name(conn, core.agent_status(conn, who))
    except Exception:  # a name it cannot work out: the plain title
        return "MaximizePM: needs you"


def compose(conn, rows):
    """One message for a batch of outbox rows on one channel. The link follows notify_open: the agent's own app
    (agent), or the page at the thing (maxpm, and agent when the agent has no web link)."""
    mode, person = open_for(conn, rows)

    def agent_url(r):
        return core.agent_web_url(conn, core.origin_agent(conn, r["item_id"], r["message_id"])) if mode == "agent" else None

    if len(rows) == 1:
        r = rows[0]
        return _title(conn, r), r["summary"], agent_url(r) or _page_link(conn, r, person)
    lines = [f"- {r['summary']}" for r in rows[:10]]
    if len(rows) > 10:
        lines.append(f"- and {len(rows) - 10} more")
    # agent: one app behind every row opens it; else the Board, with Needs you on top.
    urls = {agent_url(r) for r in rows}
    url = urls.pop() if len(urls) == 1 and None not in urls else page_url(conn, person=person)
    return f"MaximizePM: {len(rows)} things need you", "\n".join(lines), url


def _adapter(conn, channel):
    make = ADAPTERS.get(channel)
    if not make:
        raise RiverError(f"no adapter for channel {channel!r}; known: {', '.join(sorted(ADAPTERS)) or '(none)'}")
    return make(conn)


def run(conn, now_=False):
    """Send every channel batch that is due. Returns one result per channel batch it tried."""
    core.sync_needs_you_now(conn)
    t = core.now()
    by_channel = {}
    for r in core.outbox(conn):
        by_channel.setdefault(r["channel"], []).append(r)
    results = []
    for channel, rows in sorted(by_channel.items()):
        # A channel may have its own window (<channel>_batch_window, when DEFAULT_SETTINGS has one); else
        # notify_batch_window.
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
        here = core.session_url_from_env() if core.setting(conn, "notify_open") == "agent" else None
        _adapter(conn, channel)("MaximizePM: test", "This is a test notification from maxpm notify test.",
                                  here or page_url(conn))
    except RiverError:
        raise
    except Exception as e:
        return {"channel": channel, "ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"channel": channel, "ok": True}


def direct(conn, title, body, url=None):
    """Send one notice now on each channel of notify_channels (the queue's and each person's), outside the outbox.
    For the watchdog (#5441): it runs when maxpm serve, which sends the outbox, may be down; and for the status inbox
    (#5852, url: the session's page). Never raises: each channel's result is returned."""
    out = []
    people = [r["name"] for r in conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY name")]
    channels = dict.fromkeys(ch for who in [None, *people]
                             for ch in core._channels(core.setting(conn, "notify_channels", agent=who)))
    for ch in channels:
        try:
            _adapter(conn, ch)(title, body, url or page_url(conn))
        except Exception as e:
            out.append({"channel": ch, "ok": False, "error": f"{type(e).__name__}: {e}"})
        else:
            out.append({"channel": ch, "ok": True})
    return out


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
            "batch_window": core.setting(conn, "notify_batch_window"),
            "hint": mac_hint() if "mac" in configured else None,
            "open": core.setting(conn, "notify_open"), "open_people": {
                r["scope"][6:]: r["value"] for r in conn.execute(
                    "SELECT scope, value FROM settings WHERE key='notify_open' AND scope LIKE 'agent:%' ORDER BY scope")},
            "phone": phone_note(conn) if "ntfy" in configured else None}


def _say(text):
    """One line on the terminal of the loop. A terminal that is gone must not end the loop."""
    try:
        print(text, flush=True)
    except Exception:
        pass


def ops_deadman(conn):
    """maxpm serve only: the beat to the outside heartbeat service (core.ops_deadman, #5435). maxpm notify run
    sends none: the beat says that serve runs."""
    return core.ops_deadman(conn) if SERVE_PORT.get("port") else None


def sweep(conn):
    """The sweep of maxpm serve, once a pass (core.poll_activity: at most each POLL_SWEEP). The page build ran it
    before #5597, in the request of the page."""
    core.poll_activity(conn, None)


def release_check(conn):
    """maxpm serve only: ask GitHub once a day for the latest release (river/release.py)."""
    if SERVE_PORT.get("port"):
        from . import release
        release.loop_step(conn)


def loop(stop, interval_s=None):
    """Run the dispatcher until stop (a threading.Event) is set. For maxpm serve and maxpm notify run.
    No error of a pass ends the loop: the next pass tries again. After each pass it records the time and the
    error in serve-loop.json beside the queue, so the page and the manager see a loop that is late (#1663)."""
    LOOP.update(started=core.iso(core.now()), last_pass=None)
    while not stop.is_set():
        wait = interval_s or 30
        conn = error = None
        try:
            # Inside the try: a queue that is busy or cannot open fails this pass, not the thread (#1663).
            conn = core.connect()
            wait = interval_s or core.parse_duration(core.setting(conn, "notify_interval")).total_seconds()
            from . import server, space, vault
            from .routes import canvas as canvas_routes, sessions as session_routes
            # Each step in its own try: an error of one step fails that step, not the steps after it (#5075: an
            # error in adopt_sessions stopped auto_release and every later step while one agent had no pid).
            steps = (
                sweep,  # expired leases, holds and reservations, and needs-you, as every command does (#5597)
                server.watch_prompts,  # an agent that waits on a prompt in its tmux pane: tell the person
                session_routes.status_push,  # notify_status: a session entered a state the person wants a push for (#5852)
                server.paste_first_prompts,  # an agent that takes no prompt on its command line: paste it (#5221)
                server.paste_answers,  # an answer for an asker that waits at its prompt: type it there (#5303)
                server.paste_pending,  # a message held during a turn: type it at the idle prompt (#5587)
                core.activity_watch,  # an agent whose hooks reported nothing for activity_silence: tell the manager
                server.watch_busy,  # an agent that is busy with no river command keeps its leases
                core.sample_sessions,  # once a minute: what each live session does, for maxpm health (#5352)
                core.tell_watch_late,  # a manager that runs no watch while a message is due: tell the person
                core.tell_unanswered,  # a blocked message the manager did not answer in manage_unanswered: tell its sender
                server.adopt_sessions,  # tmux_adopt: a session outside tmux, quiet at its prompt, moves into a pane
                server.revive_sessions,  # a session whose process ended with no stop resumes in a pane (#5061)
                server.fresh_sessions,  # work for an agent idle at its prompt goes to a fresh session
                core.end_quiet_experts,  # an expert nobody talked with for expert_idle_end ends (#4955)
                server.auto_release,  # a ready review gets a reviewer, a ready deploy its deployer
                server.auto_operational,  # an operational task starts in a new session; old lists get a cleanup
                server.auto_context,  # goal_context on: a goal with ready work gets a context session (a base)
                server.auto_tidy,  # every tidy_every: close the tmux panes of sessions that are done
                server.auto_end_orphans,  # and end the tmux servers the tests left behind with no socket
                server.browser_sweep,  # a session's Chrome whose session ended or that idled browser_idle_end (#5636)
                space.serve_pass,  # over db_max_size or folder_max_size: the oldest removable data goes (#5711)
                server.hooks_soon,  # a hook whose command ended before it ran it
                server.ops_watch,  # the operations checks that are due, each in a thread of its own (#5430)
                canvas_routes.rqs_responses,  # comments and votes of realquicksite pages, to the canvas's session (#5790)
                core.schedule_due,  # a scheduled task whose time, less its lead, has come adds its item (#5507)
                vault.grant_summary,  # once a day: the manager's credential grants, to the person (#5828)
                ops_deadman,  # the beat to ops_deadman_url, so an outside service sees that serve runs (#5435)
                release_check,  # once a day: is a newer MaximizePM out (update_check, #5601)
                lambda c: core._channels(core.setting(c, "notify_channels")) and run(c),  # the notices
            )
            failed = []
            for step in steps:
                try:
                    step(conn)
                except Exception as e:
                    name = getattr(step, "__name__", "step")
                    name = "notify" if name == "<lambda>" else name
                    failed.append(f"{name}: {type(e).__name__}: {e}")
                    _say(f"maxpm notify: {name}: {e}")
                    try:
                        conn.rollback()  # a step that failed inside a tx leaves no open transaction for the next
                    except Exception:
                        pass
            if failed:
                error = "; ".join(failed)
            # maxpm serve only: run the new code, but not while a hook or a check runs, in this process or in another
            # process of serve (restart_blocked, #5654)
            if SERVE_PORT.get("port") and not server.restart_blocked(conn) and server.reload_ready(conn):
                if CHILD["on"]:  # a process of its own (river/loop.py, #5569): it ends, and serve restarts
                    CHILD["restart"] = True
                    stop.set()
                else:
                    conn.close()
                    server.restart_now("the MaximizePM code changed")
        except Exception as e:  # keep the loop alive; the next pass retries
            error = f"{type(e).__name__}: {e}"
            _say(f"maxpm notify: {e}")
        t = core.iso(core.now())
        if not error:
            LOOP["last_pass"] = t
        core.serve_loop_beat(conn, {**LOOP, "last_try": t, "error": error})
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        stop.wait(max(1.0, wait))
