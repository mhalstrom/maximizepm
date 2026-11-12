"""The page's state as versions and changes (#4709).

The page polls /api/state every three seconds. A full state of a large queue is many megabytes, more than the relay
carries, so the page sends the version of the state it has (?since=<v>) and gets only what changed:

    {"v": "<new version>", "state": {...}}                       no usable since: the whole state
    {"v": "<new version>", "since": "<v>", "parts": {...},         the parts that changed, whole
     "rows": {"items": {"set": [rows], "del": [ids], "order": [ids]}}, "drop": [parts]}

Lists of rows with a key (KEYED; "key" names it) change by row: "set" holds new and changed rows, "del" the keys of rows that left,
and "order" the keys in their new order, only when the order changed. Other parts come whole when they change.
static/state.js applies an answer to the page's copy.

The server keeps a signature of each version it gave (a hash per part and per row), not the state: unchanged
parts share one object between versions, so a version costs only what changed. Versions name this process, so a
page that comes back to a restarted server gets the whole state again."""

import json
import secrets
import threading
from collections import OrderedDict

# The parts the page changes by row, and the key of a row.
KEYED = {"items": "id", "agents": "name", "takeovers": "id", "needs_you": "id", "events": "id"}
# How many versions the server remembers. A page that was away for longer gets the whole state again.
KEEP = 400
# Fields of an item that the page never reads from the state: its drawer and pop-up load the item itself
# (/api/item/<id>). They are most of the bytes of an item.
ITEM_LEFT_OUT = ("context", "check", "touches")
# A done or dropped item in a list shows its title and a short line of output: longer text is cut.
CLOSED_TEXT = 300

_lock = threading.Lock()
_versions: "OrderedDict[str, dict]" = OrderedDict()
_prefix = secrets.token_hex(3)
_count = [0]


def slim(state):
    """The state as the page uses it: items without the fields it never reads; a closed item without empty fields
    and with long text cut."""
    items = []
    for it in state.get("items") or []:
        if it.get("status") in ("done", "dropped"):
            row = {k: (v[:CLOSED_TEXT] + "…" if v.__class__ is str and len(v) > CLOSED_TEXT else v)
                   for k, v in it.items() if v is not None and k not in ITEM_LEFT_OUT}
        else:
            row = dict(it)
            for k in ITEM_LEFT_OUT:
                row.pop(k, None)
        items.append(row)
    return dict(state, items=items)


def _hash(value):
    return hash(json.dumps(value, default=str))


def _signature(state, last):
    """A hash per part; for a KEYED part, a hash per row and one of the order. A part that did not change since
    the last version shares that version's object, so the remembered versions stay small."""
    sig = {}
    for name, value in state.items():
        if name in KEYED and isinstance(value, list):
            key = KEYED[name]
            rows = {r[key]: _hash(r) for r in value}
            order = hash(tuple(r[key] for r in value))
            prev = last.get(name) if last else None
            if prev and prev[0] == "k" and prev[2] == order and prev[1] == rows:
                sig[name] = prev
            else:
                sig[name] = ("k", rows, order)
        else:
            sig[name] = ("v", _hash(value))
    return sig


# The signatures of the last states answered, by the state object: the server shares one built state between the
# pages that poll within its STATE_TTL, and a hash of every row of the done view is 35 ms (#5591).
_sigs: "OrderedDict[int, tuple]" = OrderedDict()


def answer(state, since=None):
    """The answer to a page that has the version since (None or unknown: the whole state)."""
    with _lock:
        last_v = next(reversed(_versions)) if _versions else None
        last = _versions[last_v] if last_v else None
        memo = _sigs.get(id(state))
        if memo is not None and memo[0] is state:
            sig = memo[1]
        else:
            sig = _signature(state, last)
            _sigs[id(state)] = (state, sig)
            while len(_sigs) > 2:
                _sigs.popitem(last=False)
        if last is not None and sig == last:
            v = last_v
        else:
            _count[0] += 1
            v = f"{_prefix}-{_count[0]}"
            _versions[v] = sig
            while len(_versions) > KEEP:
                _versions.popitem(last=False)
        old = _versions.get(since) if since else None
    if old is None:
        return {"v": v, "state": state}
    out = {"v": v, "since": since, "parts": {}, "rows": {}}
    for name, s in sig.items():
        o = old.get(name)
        if o is s or o == s:
            continue
        if s[0] == "k" and o and o[0] == "k":
            key, cur, prev = KEYED[name], s[1], o[1]
            change = {"key": key}
            changed = [r for r in state[name] if prev.get(r[key]) != cur[r[key]]]
            gone = [k for k in prev if k not in cur]
            if changed:
                change["set"] = changed
            if gone:
                change["del"] = gone
            if s[2] != o[2]:
                change["order"] = [r[key] for r in state[name]]
            out["rows"][name] = change
        else:
            out["parts"][name] = state[name]
    drop = [name for name in old if name not in sig]
    if drop:
        out["drop"] = drop
    return out


def reset():
    """Forgets every version (tests)."""
    with _lock:
        _versions.clear()
        _sigs.clear()
