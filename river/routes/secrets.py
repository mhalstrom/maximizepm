"""Op "secret" (#5757, river/vault.py): maxpm secret from inside an agent's sandbox, which refuses the macOS
Keychain, and maxpm secret put --from-browser, which reads the value from the project's browser here, so it never
leaves maxpm serve. Also the sets and grants (#5822). The CLI is the only caller for now; the page shows no value.

GET: none. OPS: page actions, merged into server.OPS."""

import base64
import re

from .. import vault
from ..core import RiverError

GET = []

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
