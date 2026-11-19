"""What the forge sources share (#6160): the value of a pipeline's secret, one HTTP call, and whether an event was
kept already. The GitLab adapter (gitlab.py), the account checks (accounts.py) and the plugins' secrets (plugin.py)
read them here. GitHub, Gitea, Forgejo and Pulumi Delivery are shipped plugins now (plugins/github, plugins/pulumi).

A secret's value comes from the maxpm secret store (policy g6: never from the environment or a file of the home
folder) and stays TOKEN_KEEP seconds in memory: each read from the store is a line of its log. Tests replace secret()."""

import time
import urllib.error
import urllib.request

from .. import core

TOKEN_KEEP = 600
_TOKENS = {}


def secret(conn, name, use):
    """The value of the maxpm secret name, for a use that its log names."""
    got = _TOKENS.get(name)
    if got and time.monotonic() - got[1] < TOKEN_KEEP:
        return got[0]
    from .. import vault
    value = vault.get(conn, name, use=use).decode().strip()
    _TOKENS[name] = (value, time.monotonic())
    return value


def forget(name):
    """Read the secret name from the store again at its next use (a new token saved under the same name)."""
    _TOKENS.pop(name, None)


def token(conn, p):
    """The API token of pipeline p's forge, from the maxpm secret that p names."""
    from .runner import FORGES
    name = FORGES.get(p["forge"], "GitHub")
    if not p["secret"]:
        raise core.RiverError(f"pipeline {p['name']} names no secret for its {name} token: maxpm pipeline forge "
                              f"{p['name']} {p['forge'] or 'github'} --repo {p['repo'] or 'owner/name'} --secret <name>")
    return secret(conn, p["secret"], f"pipeline {p['name']}: the {name} API")


def fetch(method, url, headers, body):
    """One HTTP call: (status, bytes)."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def seen_by(conn, source, event_id):
    """Whether the event (source, id) is kept already."""
    return conn.execute("SELECT 1 FROM pipeline_events WHERE source=? AND event_id=?", (source, event_id)).fetchone()
