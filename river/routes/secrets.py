"""Op "secret" (#5757, river/vault.py): maxpm secret from inside an agent's sandbox, which refuses the macOS
Keychain, and maxpm secret put --from-browser, which reads the value from the project's browser here, so it never
leaves maxpm serve. Also the sets and grants (#5822), and the Credentials panel of Settings (#5829), which shows
names and never a value: a password that the person types there goes to the Keychain and never comes back.

GET: /api/credentials. OPS: page actions, merged into server.OPS."""

import base64
import re

from .. import core, vault
from ..core import RiverError

# What the Credentials panel shows of a credential: names, levels, use; never a value.
SHOWN = ("name", "kind", "site", "account", "two_step", "fail_why", "level", "var", "allow_env", "allow_commands",
         "allow_hosts", "used_at", "used_by", "uses", "sets", "note", "project", "ask")


def credentials(h, path):
    """GET /api/credentials: the credentials (names only), the sets, the grants that hold now, and the choices of a
    grant, in one read (#5829). It changes nothing, so a read of the page refreshes nothing."""
    conn = core.connect()
    try:
        rows = [{k: r.get(k) for k in SHOWN} for r in vault.listing(conn)["secrets"]]
        out = {"credentials": rows, "sets": vault.sets(conn)["sets"], "grants": vault.grants(conn)["grants"],
               "requests": vault.requests(conn)["requests"],  # #5834: the checklists that wait (#5833 draws them)
               "levels": list(vault.LEVELS), "scopes": list(vault.GRANT_SCOPES), "kinds": list(vault.GRANT_KINDS),
               "store": vault.backend().name, "browsers": session_browsers()}
        signin_accounts(conn, out["requests"])
    finally:
        conn.close()
    return h._send(200, out)


def signin_accounts(conn, reqs):
    """Each sign-in request gets 'accounts': {site: [the accounts that maxpm browser accounts lists there]}, so the
    modal names the account whose sign-in the person gives (#5840)."""
    from .. import browser
    for r in reqs:
        if r.get("sites"):
            pairs = browser.accounts(conn, (r.get("source") or "").partition("/")[0] or None)
            r["accounts"] = {s: [a for site, a in pairs if browser.on_site(s, site) or browser.on_site(site, s)]
                             for s in r["sites"]}


def session_browsers():
    """The session browsers that serve runs now, as [{'session', 'project'}] (#5840): the Credentials panel saves
    the sign-in of one of them into a set (action cookies), so the person types no terminal command."""
    from .. import browser  # here, not at the top: the CLI calls act() and needs no browser code
    return [{"session": owner, "project": project}
            for (project, owner), b in sorted(list(browser.BROWSERS.items()), key=lambda kv: (kv[0][1] or "", kv[0][0]))
            if owner and (b.running() or b.proxy is not None)]


GET = [(lambda path: path == "/api/credentials", credentials)]

# The value of an element: a field's value, else its text.
READ_JS = ("(el) => (typeof el.value === 'string' && el.tagName !== 'BUTTON' ? el.value"
           " : (el.innerText || el.textContent || ''))")


def from_browser(conn, a, who):
    """The text of the element that a['from_browser'] names (a name as the snapshot prints it, a ref, or
    css:<selector>; 'page' or empty: the page's whole text), cut by a['match'] (a regex: its first group, else the
    whole match)."""
    from .. import browser  # here, not at the top: the CLI calls act() and needs no browser code
    from . import browser as routes_browser
    b =routes_browser._browser_of(conn, a, who)
    if not b.running():
        raise RiverError("the project's browser is not running: open the page with the value first "
                         "(maxpm browser open <address>)")
    target = (a.get("from_browser") or "").strip()
    if target in ("", "page"):
        text = b.evaluate("document.body ? document.body.innerText : ''")
    else:
        sel = routes_browser._resolve(conn, b, None, "read", target, a.get("within"), verb="read")
        text = browser.on_element(b, sel, READ_JS)
    text = text if isinstance(text, str) else ""
    if a.get("match"):
        try:
            m = re.search(a["match"], text)
        except re.error as e:
            raise RiverError(f"--match is not a regex ({e})")
        if not m:
            raise RiverError("--match finds nothing in that text: nothing was saved (maxpm browser text shows the page;"
                             " a value that the page shows as dots needs its field or its copy button's source)")
        text = m.group(1) if m.re.groups else m.group(0)
    text = text.strip()
    if not text:
        raise RiverError("the browser gives an empty text there: nothing was saved")
    return text.encode()


def approve_signins(conn, a, who):
    """Allow of a sign-in request (#5840): for each ticked site, the cookies of the request's source browser are saved
    as a saved sign-in (r<id>-<site>), and approve grants exactly those, as a set of their own, to the request's task
    or session. One step, by serve, as the person's act; a site with no cookies there refuses the whole Allow.
    always: the ticked sites whose new saved sign-in is 'Allow without asking' from now on (#5842, the modal's 'Do
    not ask again for this site'). The manager's approve grants the saved sign-ins that the person allows without
    asking, and saves nothing new."""
    from .. import browser, signin
    r = vault.request_info(conn, vault._request(conn, a.get("request")))
    if r["state"] not in ("open", "asked"):
        raise RiverError(f"request {r['request']} is {r['state']}")
    pick = r["ticked"] if a.get("only") is None else [vault.norm_site(s) for s in a["only"] if s.strip()]
    if not pick:
        raise RiverError(f"nothing is ticked: tick the sites to allow, or Deny (maxpm secret refuse {r['request']})")
    wrong = [s for s in pick if s not in r["sites"]]
    if wrong:
        raise RiverError(f"{', '.join(wrong)}: not asked in {r['request']} (it asks for {', '.join(r['sites'])})")
    if not vault.session_of(conn, who)["person"]:
        asked = [s for s in pick if not vault.free_signin(conn, s)]
        if asked:  # vault.approve refuses anyone but the manager; for the manager, name the sites that ask
            vault._person_only(conn, who, f"allow the sign-ins of {', '.join(asked)} (Ask me)")
        return {**vault.approve(conn, r["id"], [vault.free_signin(conn, s) for s in pick], who, a.get("note")),
                "saved": []}
    always = {vault.norm_site(s) for s in (a.get("always") or []) if s.strip()}
    project, _, session = (r["source"] or "").partition("/")
    if not conn.execute("SELECT 1 FROM projects WHERE name=?", (project,)).fetchone():
        raise RiverError(f"request {r['request']} names no project of a browser: ask again with --from")
    b = browser.get(project, session or None, conn)
    if not b.running():  # it stopped (browser_idle_end) or serve started again: its profile on disk keeps the
        b.start()        # sign-in, so start it (or attach to the Chrome that the earlier serve left running)
    got = {}
    for site in pick:
        got[site] = signin.site_cookies(b, site)
        if not got[site]:
            raise RiverError(f"the browser of {session or project} has no sign-in of {site}: sign in there, then "
                             "Allow again")
    names = []
    for site, cookies in got.items():
        names.append(vault.cookies_save(conn, f"{r['request']}-{site}", site, cookies, who)["secret"])
        if site in always:
            vault.set_ask(conn, names[-1], "allow", who)
    return {**vault.approve(conn, r["id"], names, who, a.get("note")), "saved": names}


def act(conn, a, who):
    action = a.get("action")
    name = a.get("name")
    if action == "put":
        if a.get("from_browser") is not None:
            data, how = from_browser(conn, a, who), "browser"
        else:
            data, how = base64.b64decode(a.get("value_b64") or ""), a.get("how") or "stdin"
        return vault.put(conn, name, data, who, a.get("project"), a.get("note"), how, a.get("var"))
    if action == "get":
        data = vault.get(conn, name, who, a.get("use"), a.get("how"), a.get("argv"))
        return {"secret": name, "value_b64": base64.b64encode(data).decode(), **vault.protection(conn, name)}
    # Requests and their checklists (#5834)
    if action == "request":
        if a.get("sites"):  # #5840: sign-ins from a session's browser, at the person at once
            return vault.signin_request(conn, a["sites"], a.get("source"), a.get("reason"), who, a.get("item"))
        return vault.request(conn, name, a.get("reason"), who, a.get("item"))
    if action == "propose":
        return vault.propose(conn, a.get("request"), a.get("offer") or [], a.get("tick") or [], a.get("reason"), who)
    if action == "approve" and vault._request(conn, a.get("request"))["sites"]:
        return approve_signins(conn, a, who)
    if action == "approve":
        return vault.approve(conn, a.get("request"), a.get("only"), who, a.get("note"))
    if action == "refuse":
        return vault.refuse(conn, a.get("request"), a.get("note"), who)
    if action == "requests":
        return vault.requests(conn, bool(a.get("all")))
    if action == "check":
        return vault.check(conn, a.get("agent") or name)
    if action == "cookies":  # #5827: a session browser's sign-in of one site into a set; serve holds the browsers
        from .. import signin
        from . import browser as routes_browser
        if not a.get("session"):
            raise RiverError("name the session whose browser is signed in: --session <agent>")
        b = routes_browser._browser_of(conn, a, who)
        if not b.running():
            raise RiverError(f"the browser of {a['session']} is not running: sign in there first")
        site = vault.norm_site(a.get("site"))
        cookies = signin.site_cookies(b, site)
        if not cookies:
            raise RiverError(f"the browser of {a['session']} has no cookies of {site}: sign in there first")
        return vault.cookies_save(conn, name, site, cookies, who, a.get("set"))
    if action == "signin":  # #5826
        dec = lambda k: base64.b64decode(a[k]) if a.get(k) else None  # noqa: E731
        return vault.signin_save(conn, name, a.get("site"), a.get("account"), dec("value_b64"), dec("totp_b64"),
                                 a.get("two_step"), bool(a.get("reset")), who, a.get("project"), a.get("note"))
    if action == "level":  # #5825
        return vault.set_level(conn, name, a.get("level"), a.get("add_commands") or (), a.get("remove_commands") or (),
                               a.get("add_hosts") or (), a.get("remove_hosts") or (), a.get("env"), a.get("var"), who)
    if action == "asking":  # #5842: 'ask' (Ask me) or 'allow' (Allow without asking)
        return vault.set_ask(conn, name, a.get("ask"), who)
    if action == "delete":
        return vault.delete(conn, name, who)
    if action == "list":
        return vault.listing(conn, a.get("project"))
    if action == "log":
        return vault.log(conn, name, a.get("limit") or 30)
    # Sets and grants (#5822): names and scopes only, never a value.
    if action == "set":
        return vault.set_change(conn, name, a.get("add") or (), a.get("remove") or (), a.get("note"),
                                a.get("manager_may_grant"), bool(a.get("delete")), who)
    if action == "sets":
        return vault.sets(conn)
    if action == "grant":
        return vault.grant(conn, name, a.get("scope"), a.get("value"), a.get("until"), a.get("note"), who)
    if action == "revoke":
        return vault.revoke(conn, a.get("grant"), name, a.get("scope"), a.get("value"), who)
    if action == "grants":
        return vault.grants(conn, name, bool(a.get("ended")), a.get("for_agent"))
    raise RiverError(f"secret: no action {action!r}")


OPS = {
    "secret": act,
}
