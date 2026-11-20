"""Accounts of pipelines (#6147): each forge token is an account.

A pipeline on a forge names a maxpm secret that holds its API token (maxpm pipeline forge <p> github --secret
<name>). Several pipelines can name the same token (mark 2026-11-18: one token for each GitHub account, such as
github-mhalstrom). An account is one token on one forge server: (forge, host, secret). This module asks the forge
who the token is, when it runs out and which of the pipelines' repositories it reaches, and keeps the answer in
pipeline_accounts. The token's value never leaves the call.

What each forge says:
- GitHub: GET /user (the login, User or Organization), the header GitHub-Authentication-Token-Expiration (sent for
  a token that runs out), the header X-OAuth-Scopes (a classic token), and GET /repos/<repo> for each repository.
- Gitea and Forgejo: GET /api/v1/user and /api/v1/repos/<repo>; no end date.
- GitLab: GET /api/v4/personal_access_tokens/self (its end date and scopes; also for project and group tokens),
  GET /api/v4/user, and /api/v4/projects/<group%2Fproject>.
- Pulumi Cloud: GET /api/user; no end date and no repositories.

maxpm serve checks each account once an hour (CHECK_EVERY), and at once when a read of a pipeline that uses it is
refused (401 or 403). The manager gets an alert at the first refusal and 7 days before the token runs out
(EXPIRY_WARN); the person makes a new token only when the manager cannot.

FETCH is a test hook: fn(method, url, headers) -> (status, headers dict, body bytes).
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .. import core

CHECK_EVERY = 3600  # seconds between two checks of one account
RECHECK_MIN = 300  # a refused read checks the account at once, but not more than once in this time
EXPIRY_WARN = 7  # days before the end of a token that the manager gets its alert
FORGE_NAMES = {"github": "GitHub", "gitlab": "GitLab", "gitea": "Gitea", "forgejo": "Forgejo",
               "pulumi": "Pulumi Cloud"}
FETCH = None
_REFUSED = re.compile(r"answered (401|403)\b")


def _host(p):
    if p["forge_url"]:
        return urllib.parse.urlparse(p["forge_url"]).netloc or p["forge_url"]
    return {"github": "github.com", "gitlab": "gitlab.com", "pulumi": "api.pulumi.com"}.get(p["forge"], p["forge"])


def key_of(p):
    """The account of pipeline p: (forge, host, secret), or None (no forge, or a plugin: its secrets are its own)."""
    if p["forge"] not in FORGE_NAMES:
        return None
    return p["forge"], _host(p), p["secret"] or ""


def _fetch(method, url, headers):
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()


def _get(p, token, path):
    """(status, headers, JSON or None) of one GET on the forge of pipeline p."""
    forge = p["forge"]
    if forge == "github":
        base, auth = "https://api.github.com", {"Authorization": f"Bearer {token}",
                                                "X-GitHub-Api-Version": "2022-11-28"}
    elif forge in ("gitea", "forgejo"):
        base, auth = (p["forge_url"] or "").rstrip("/") + "/api/v1", {"Authorization": f"token {token}"}
    elif forge == "gitlab":
        base, auth = (p["forge_url"] or "https://gitlab.com").rstrip("/") + "/api/v4", {"PRIVATE-TOKEN": token}
    else:
        base, auth = (p["forge_url"] or "https://api.pulumi.com").rstrip("/"), {"Authorization": f"token {token}"}
    status, headers, body = (FETCH or _fetch)("GET", base + path, {**auth, "Accept": "application/json",
                                                                    "User-Agent": "maxpm-pipeline"})
    try:
        data = json.loads(body) if body else None
    except ValueError:
        data = None
    return status, headers, data


def _message(data):
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or "")[:300]
    return ""


def _github_end(text):
    """GitHub-Authentication-Token-Expiration: '2026-12-31 00:00:00 UTC' (an offset such as '+0100' also occurs)."""
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            t = datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
        t = t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
        return core.iso(t)
    return None


def _ask(p, token, repos):
    """What the forge says about the token: {'owner', 'kind', 'expires_at', 'scopes', 'repos'}; refuses with the
    forge's words, and 'refused' in the result when the forge answered 401 or 403."""
    forge = p["forge"]
    name = FORGE_NAMES[forge]
    out = {"owner": None, "kind": None, "expires_at": None, "scopes": None, "repos": {}}
    if forge == "gitlab":
        st, _h, tok = _get(p, token, "/personal_access_tokens/self")
        if st in (401, 403):
            return {"refused": f"{name} answered {st}: {_message(tok) or 'the token is not valid'}"}
        if st == 200 and isinstance(tok, dict):
            out["expires_at"] = f"{tok['expires_at']}T00:00:00Z" if tok.get("expires_at") else None
            out["scopes"] = ", ".join(tok.get("scopes") or []) or None
    path = {"github": "/user", "gitea": "/user", "forgejo": "/user", "gitlab": "/user", "pulumi": "/api/user"}[forge]
    st, headers, me = _get(p, token, path)
    if st in (401, 403):
        return {"refused": f"{name} answered {st}: {_message(me) or 'the token is not valid'}"}
    if st != 200 or not isinstance(me, dict):
        raise core.RiverError(f"{name} answered {st} to {path}: {_message(me)}")
    out["owner"] = me.get("login") or me.get("username") or me.get("githubLogin") or me.get("name")
    t = str(me.get("type") or "").lower()
    out["kind"] = "organization" if t == "organization" else "bot" if me.get("bot") or t == "bot" else "user"
    if forge == "github":
        out["expires_at"] = _github_end(headers.get("github-authentication-token-expiration"))
        out["scopes"] = headers.get("x-oauth-scopes") or None
    for repo in repos:
        if forge == "pulumi":
            break  # a Delivery pipeline names a stack, which /api/user does not list
        path = (f"/projects/{urllib.parse.quote(repo, safe='')}" if forge == "gitlab" else f"/repos/{repo}")
        st, _h, _d = _get(p, token, path)
        out["repos"][repo] = st == 200
    return out


# ---------------------------------------------------------------- reads

def _users(conn):
    """{account key: [pipeline rows]} for the pipelines on a forge."""
    out = {}
    for p in conn.execute("SELECT * FROM pipelines ORDER BY name").fetchall():
        k = key_of(p)
        if k:
            out.setdefault(k, []).append(p)
    return out


def _row(conn, k):
    return conn.execute("SELECT * FROM pipeline_accounts WHERE forge=? AND host=? AND secret=?", k).fetchone()


def _days_left(expires_at):
    if not expires_at:
        return None
    return (core.parse_iso(expires_at) - core.now()).total_seconds() / 86400


def _view(conn, k, users, saved):
    """One account as the page and the command show it."""
    forge, host, secret = k
    r = _row(conn, k)
    refused_read = next((p["poll_error"] for p in users if p["poll_error"] and _REFUSED.search(p["poll_error"])), None)
    if not secret or secret not in saved:
        state = "missing"
        error = (f"{', '.join(p['name'] for p in users)} names no token" if not secret else
                 f"{', '.join(p['name'] for p in users)} {'names' if len(users) == 1 else 'name'} the token "
                 f"{secret}, but nobody saved it yet")
    elif (r and r["state"] == "refused") or refused_read:
        state, error = "refused", (r["error"] if r and r["state"] == "refused" else refused_read)
    elif r and r["state"] == "error":
        state, error = "error", r["error"]
    elif r and r["state"] == "ok":
        state, error = "ok", None
    else:
        state, error = "new", None
    reads = [p["polled_at"] for p in users if p["polled_at"] and not p["poll_error"]]
    left = _days_left(r["expires_at"]) if r else None
    return {"forge": forge, "label": FORGE_NAMES[forge], "host": host, "secret": secret or None,
            "owner": r["owner"] if r else None, "kind": r["kind"] if r else None,
            "expires_at": r["expires_at"] if r else None, "days_left": None if left is None else round(left, 1),
            "repos": json.loads(r["repos"]) if r and r["repos"] else {}, "scopes": r["scopes"] if r else None,
            "state": state, "error": error, "checked_at": r["checked_at"] if r else None,
            "refused_since": (r["refused_since"] if r else None) if state == "refused" else None,
            "read_at": max(reads) if reads else None, "used_by": [p["name"] for p in users]}


def listing(conn):
    """maxpm pipeline accounts: each account that a pipeline uses, with what its forge said."""
    saved = {r["name"] for r in conn.execute("SELECT name FROM secrets")}
    return [_view(conn, k, users, saved) for k, users in sorted(_users(conn).items())]


def for_pipeline(conn, p):
    """The account of pipeline p (a row of pipelines), or None."""
    k = key_of(p)
    if not k:
        return None
    users = [x for x in conn.execute("SELECT * FROM pipelines").fetchall() if key_of(x) == k]
    saved = {r["name"] for r in conn.execute("SELECT name FROM secrets")}
    return _view(conn, k, users, saved)


# ---------------------------------------------------------------- checks

def check(conn, k, users=None):
    """Ask the forge about account k now and keep its answer. Returns the account as listing shows it."""
    from . import forge
    users = users if users is not None else _users(conn).get(k, [])
    if not users:
        raise core.RiverError(f"no pipeline uses the {FORGE_NAMES.get(k[0], k[0])} token {k[2]}")
    p = users[0]
    now = core.iso(core.now())
    with core.tx(conn):
        conn.execute("INSERT OR IGNORE INTO pipeline_accounts(forge, host, secret) VALUES (?,?,?)", k)
    saved = {r["name"] for r in conn.execute("SELECT name FROM secrets")}
    if not k[2] or k[2] not in saved:
        return _view(conn, k, users, saved)
    repos = sorted({x["repo"] for x in users if x["repo"]})
    try:
        got = _ask(p, forge.token(conn, p), repos)
    except (core.RiverError, OSError, ValueError) as e:
        with core.tx(conn):
            conn.execute("UPDATE pipeline_accounts SET state='error', error=?, checked_at=? WHERE forge=? AND host=? "
                         "AND secret=?", (str(e)[:500], now, *k))
        return _view(conn, k, users, saved)
    with core.tx(conn):
        if "refused" in got:
            conn.execute("UPDATE pipeline_accounts SET state='refused', error=?, checked_at=?, "
                         "refused_since=COALESCE(refused_since, ?) WHERE forge=? AND host=? AND secret=?",
                         (got["refused"], now, now, *k))
        else:
            conn.execute("UPDATE pipeline_accounts SET state='ok', error=NULL, checked_at=?, refused_since=NULL, "
                         "alert_refused=NULL, owner=?, kind=?, expires_at=?, repos=?, scopes=? WHERE forge=? AND "
                         "host=? AND secret=?", (now, got["owner"], got["kind"], got["expires_at"],
                                                 json.dumps(got["repos"]), got["scopes"], *k))
    if "refused" in got:
        forge.forget(k[2])  # a new token saved under the same name is read anew, not from memory
    return _view(conn, k, users, saved)


def check_now(conn, secret=None, actor=None):
    """maxpm pipeline accounts --check [--secret <name>]: check the accounts (or one) now. A person or the manager."""
    why = core._lead_refusal(conn, actor, "checks the tokens of pipelines with their forges")
    if why:
        raise core.RiverError(why)
    users = _users(conn)
    keys = [k for k in users if secret is None or k[2] == secret]
    if not keys:
        raise core.RiverError(f"no pipeline uses the token {secret}" if secret else "no pipeline uses a Git host token")
    return [check(conn, k, users[k]) for k in keys]


def _alert(conn, text):
    mgr = core.active_manager(conn)
    if not mgr:
        return False
    with core.tx(conn):
        core._send(conn, "alert", "maxpm", text, to=mgr)
    return True


def serve_pass(conn):
    """One pass of maxpm serve: check each account that is due, then send the manager the alerts that are due: the
    first refusal of a token, and a token that runs out within EXPIRY_WARN days. Returns {'checked', 'alerts'}."""
    now = core.now()
    checked, alerts = [], []
    saved = {r["name"] for r in conn.execute("SELECT name FROM secrets")}
    for k, users in _users(conn).items():
        r = _row(conn, k)
        last = core.parse_iso(r["checked_at"]) if r and r["checked_at"] else None
        refused_read = any(p["poll_error"] and _REFUSED.search(p["poll_error"]) for p in users)
        due = last is None or (now - last).total_seconds() >= CHECK_EVERY or (
            refused_read and r["state"] != "refused" and (now - last).total_seconds() >= RECHECK_MIN)
        if due and k[2] and k[2] in saved:
            check(conn, k, users)
            checked.append(k[2])
        a = _view(conn, k, users, saved)
        r = _row(conn, k)
        names = ", ".join(a["used_by"])
        if a["state"] == "refused" and r is not None and not r["alert_refused"]:
            if _alert(conn, f"{a['label']} refuses the token {k[2]} ({a['error']}). The pipelines {names} show old "
                            f"data and start nothing on {a['label']} until a new token works. Make a new token for "
                            f"{a['owner'] or 'that account'} and save it: maxpm secret put {k[2]}; ask the person only "
                            f"for the sign-in on {a['label']}"):
                with core.tx(conn):
                    conn.execute("UPDATE pipeline_accounts SET alert_refused=? WHERE id=?", (core.iso(now), r["id"]))
                alerts.append(("refused", k[2]))
        left = a["days_left"]
        if a["state"] == "ok" and left is not None and left <= EXPIRY_WARN and r["alert_expiry"] != r["expires_at"]:
            when = "ran out" if left < 0 else f"runs out in {max(0, int(left))} day(s)"
            if _alert(conn, f"The {a['label']} token {k[2]} of {a['owner'] or 'its account'} {when} "
                            f"({r['expires_at'][:10]}); the pipelines {names} use it. Make a new token before then and "
                            f"save it under the same name: maxpm secret put {k[2]}"):
                with core.tx(conn):
                    conn.execute("UPDATE pipeline_accounts SET alert_expiry=? WHERE id=?", (r["expires_at"], r["id"]))
                alerts.append(("expiry", k[2]))
    return {"checked": checked, "alerts": alerts}


# ---------------------------------------------------------------- a new token from the page (#6148)

# Where a person makes a token on each forge, and what it needs (canvas deploy-tab-mockup picture 8).
NEW_TOKEN_PAGE = {"github": "https://github.com/settings/personal-access-tokens/new",
                  "gitlab": "{url}/-/user_settings/personal_access_tokens",
                  "gitea": "{url}/user/settings/applications", "forgejo": "{url}/user/settings/applications",
                  "pulumi": "https://app.pulumi.com/account/tokens"}
NEEDS = {"github": "a fine-grained token for the repositories of the pipelines, with Actions: read and write (to start "
                   "a workflow after a transition), Deployments: read, Contents: read, Metadata: read",
         "gitlab": "a personal or project access token with the scope api",
         "gitea": "a token with repository: read and write", "forgejo": "a token with repository: read and write",
         "pulumi": "a personal or organization access token"}


def _forge_args(forge, url):
    if forge not in FORGE_NAMES:
        raise core.RiverError(f"the Git host is one of{', '.join(FORGE_NAMES)}, not {forge!r}")
    url = (url or "").strip().rstrip("/") or None
    if forge in ("gitea", "forgejo") and not url:
        raise core.RiverError(f"name the address of the {FORGE_NAMES[forge]} server: https://<host>")
    if url and not re.match(r"^https://[\w.-]+(:\d+)?(/[\w.-]+)*$", url):
        raise core.RiverError("the server's address is https://<host>[/<path>]")
    return url


def add_token(conn, forge, token, name=None, url=None, actor=None):
    """Check a token with its forge and save it (Deploy > Accounts > Add a token): the forge says who it is before
    anything is saved, so a wrong token never goes into the list. name: the maxpm secret it is saved as; none: <forge>-
    <owner>. The value goes only to the forge and the vault (level hidden: no command prints it); the answer has the
    account, never the value. A person, on the page."""
    from .. import vault
    url = _forge_args(forge, url)
    token = (token or "").strip()
    if not token:
        raise core.RiverError("paste the token")
    p = {"forge": forge, "forge_url": url}
    try:
        got = _ask(p, token, [])
    except OSError as e:
        raise core.RiverError(f"{FORGE_NAMES[forge]} did not answer: {e}") from e
    if "refused" in got:
        raise core.RiverError(f"{got['refused']}. Nothing was saved.")
    owner = got["owner"] or "unknown"
    name = (name or "").strip() or re.sub(r"[^a-z0-9._-]+", "-", f"{forge}-{owner}".lower()).strip("-")
    vault.check_name(name)
    saved = vault.put(conn, name, token.encode(), actor, note=f"{FORGE_NAMES[forge]} token of {owner}", how="page")
    from . import forge as forges
    forges.forget(name)  # a pipeline that read the old value reads the new one
    k = (forge, _host(p), name)
    with core.tx(conn):
        conn.execute("INSERT OR IGNORE INTO pipeline_accounts(forge, host, secret) VALUES (?,?,?)", k)
        conn.execute("UPDATE pipeline_accounts SET state='ok', error=NULL, checked_at=?, refused_since=NULL, "
                     "alert_refused=NULL, owner=?, kind=?, expires_at=?, scopes=? WHERE forge=? AND host=? AND secret=?",
                     (core.iso(core.now()), got["owner"], got["kind"], got["expires_at"], got["scopes"], *k))
    users = _users(conn).get(k)
    if users:
        check(conn, k, users)  # the repositories of the pipelines that name it
    return {"secret": name, "forge": forge, "label": FORGE_NAMES[forge], "host": _host(p), "owner": got["owner"],
            "kind": got["kind"], "expires_at": got["expires_at"], "scopes": got["scopes"], "replaced": saved["replaced"],
            "used_by": [x["name"] for x in users or []]}


def make_token(conn, forge, name, url=None, project=None, actor=None, start=True):
    """'Make the token for me': a task for a session that opens the forge's new-token page in the project's browser,
    fills in the name and the permissions, lets the person sign in and press Generate, and saves the token unseen
    (maxpm secret put --from-browser). With start, a session starts on it now. A person, on the page."""
    url = _forge_args(forge, url)
    name = (name or "").strip()
    if not name:
        raise core.RiverError(f"name the token first, such as {forge}-<account>")
    from .. import vault
    vault.check_name(name)
    if not project:
        r = conn.execute("SELECT project FROM pipelines WHERE forge=? AND secret=? AND project IS NOT NULL ORDER BY name "
                         "LIMIT 1", (forge, name)).fetchone() or conn.execute(
            "SELECT project FROM pipelines WHERE project IS NOT NULL ORDER BY name LIMIT 1").fetchone()
        project = r["project"] if r else None
    if not project:
        raise core.RiverError("name the project whose browser makes the token: no pipeline names a project")
    page = NEW_TOKEN_PAGE[forge].format(url=url or "")
    repos = sorted({r["repo"] for r in conn.execute("SELECT repo FROM pipelines WHERE forge=? AND secret=? AND repo IS "
                                                     "NOT NULL", (forge, name))})
    label = FORGE_NAMES[forge]
    context = (f"The person asked for a new {label} token on the page (Deploy > Accounts > Make the token for me). "
               f"Make it in the project's browser and save it as the maxpm secret {name}; nobody sees its value.\n"
               f"1. maxpm browser open {page}\n"
               f"2. When the page asks for a sign-in, the person signs in: maxpm ask the person to sign in to {label} in "
               f"the browser on the Sessions tab (policy p157: do every click yourself; never type a password).\n"
               f"3. Fill in the form: the name 'maxpm {name}', an end date in 90 days, "
               + (f"the repositories {', '.join(repos)}, " if repos else "")
               + f"and the permissions: {NEEDS[forge]}.\n"
               f"4. Ask the person to press Generate (maxpm ask ... --item <this task>), and wait for the answer.\n"
               f"5. Save it unseen: maxpm secret put {name} --from-browser \"<the field or text that shows the token>\" "
               f"--note \"{label} token for the pipelines\"\n"
               f"6. Check it: maxpm pipeline accounts --check --secret {name}; the Accounts tab shows its owner and end "
               f"date. Then close this task.")
    item = core.item_add(conn, project, f"Make a {label} token and save it as {name}", doer="ai", actor=actor,
                         context=context, short=f"Make the {label} token")
    out = {"item": item["id"], "started": None, "page": page}
    if start:
        from .. import server
        out["started"] = server.dispatch_item(conn, item["id"], actor=actor)
    return out
