"""What a pipeline's agents may do with no permission prompt (#6154).

mark 2026-11-18 (canvas release-tracking, goal part 2, task I): an agent that a trigger rule starts on a runner has no
person at a terminal, so no permission prompt. Each pipeline has one list in three parts:

    allow   what its agents do at once                     maxpm pipeline permit <p> --allow "Bash(npm test:*)"
    ask     what they ask first: a card, never a prompt    maxpm pipeline permit <p> --ask "Bash(./deploy.sh:*)"
    never   what they never do                             maxpm pipeline permit <p> --never "Bash(rm:*)"

Each entry is a permission rule in Claude Code's form: a tool (Read, Edit, Write, Glob, Grep, WebFetch), or Bash with
a command: Bash(git push:*) is each command that starts with 'git push', Bash(make deploy) only that command.

How a run uses it (runners.py, Claude Code): the list becomes the run's own flags, never a file in the CLI's home
(policy p140): --allowedTools with allow, --disallowedTools with ask and never, and --permission-mode dontAsk, so a
step outside allow is refused at once, never asked. The run's prompt lists the three parts. After the run, MaximizePM
reads the refused steps from the result line (permission_denials) and checks each one (outcome):
- a step of never: a line of the pipeline's audit log, nothing more;
- a step of ask, or a step on no list: a card (a person's item that the task waits on; the manager sees it first, as
  every person item). A yes (the card done) runs the task again on the same runner with that one step allowed for that
  task; a no (the card dropped) ends there, and the task goes back to the queue.
Codex has no list of tools: its run gets the list in its prompt only, and MaximizePM says so on the run.

Who: a person or the manager changes a list. Each change is a line of the audit log and a version of the definition
(define._change). The list is part of maxpm pipeline export.

Standard library only."""

from __future__ import annotations

import fnmatch
import json
import re

from .. import core
from . import audit, store

PARTS = ("allow", "ask", "never")
RULE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\(.{1,300}\))?$")
MUST = "Bash(maxpm:*)"  # an agent reports through maxpm: never asked, never refused
# The list of a pipeline that names none: what a runner allowed before (runners.ALLOW).
DEFAULT = {"allow": ["Bash(maxpm:*)", "Bash(git:*)", "Read", "Edit", "Write", "Glob", "Grep"], "ask": [], "never": []}


def check_rule(rule):
    rule = " ".join(str(rule or "").split())
    if not RULE.match(rule):
        raise core.RiverError(f"a permission is a tool (Read, Edit) or a tool with what it acts on, as Claude Code "
                              f"writes it: Bash(git push:*), Bash(make deploy), WebFetch(domain:example.com); not "
                              f"{rule!r}")
    return rule


def of(p):
    """The list of a pipeline row: {allow, ask, never}, and whether it is the default."""
    if p["permissions"]:
        d = json.loads(p["permissions"])
        return {k: list(d.get(k) or []) for k in PARTS} | {"default": False}
    return {k: list(v) for k, v in DEFAULT.items()} | {"default": True}


def show(conn, pipeline):
    p = store._pipeline(conn, pipeline)
    asks = [dict(r) for r in conn.execute("SELECT * FROM pipeline_permit_asks WHERE pipeline_id=? ORDER BY id DESC "
                                          "LIMIT 20", (p["id"],))]
    return {"pipeline": p["name"], **of(p), "asks": asks}


def change(conn, pipeline, allow=(), ask=(), never=(), remove=(), reset=False, actor=None):
    """Add rules to a part (a rule moves out of the part it was in), remove rules, or go back to the default list."""
    from . import define
    define._may(conn, actor)
    p = store._pipeline(conn, pipeline)
    cur = of(p)
    if reset:
        new = {k: list(v) for k, v in DEFAULT.items()}
    else:
        new = {k: list(cur[k]) for k in PARTS}
        gone = [check_rule(x) for x in remove]
        for k in PARTS:
            new[k] = [x for x in new[k] if x not in gone]
        for part, rules in (("allow", allow), ("ask", ask), ("never", never)):
            for x in rules:
                x = check_rule(x)
                if x == MUST and part != "allow":
                    raise core.RiverError(f"{MUST} stays allowed: an agent reports through maxpm (its steps, its "
                                          "questions, its done)")
                for k in PARTS:
                    if x in new[k] and k != part:
                        new[k].remove(x)
                if x not in new[part]:
                    new[part].append(x)
    if MUST not in new["allow"]:
        new["allow"].insert(0, MUST)
    if new == {k: cur[k] for k in PARTS}:
        raise core.RiverError(f"pipeline {p['name']} has this list already (maxpm pipeline permissions {p['name']})")
    with core.tx(conn):
        conn.execute("UPDATE pipelines SET permissions=? WHERE id=?",
                     (None if reset else json.dumps(new, separators=(",", ":")), p["id"]))
        words = "; ".join(f"{k}: {', '.join(new[k]) or 'nothing'}" for k in PARTS)
        define._change(conn, p["id"], actor, ("permissions back to the default: " if reset else "permissions: ") + words,
                       new)
    p = store._pipeline(conn, pipeline)
    return {"pipeline": p["name"], **of(p)}


def set_whole(conn, pipeline, lst, actor=None):
    """The whole list at once (a restore of a definition version, #6143)."""
    from . import define
    define._may(conn, actor)
    p = store._pipeline(conn, pipeline)
    new = {k: list(dict.fromkeys(check_rule(x) for x in lst.get(k) or [] if str(x).strip())) for k in PARTS}
    both = [x for x in new["allow"] + new["ask"] + new["never"]
            if (x in new["allow"]) + (x in new["ask"]) + (x in new["never"]) > 1]
    if both:
        raise core.RiverError(f"a rule is on one list only: {', '.join(dict.fromkeys(both))}")
    if MUST in new["ask"] + new["never"]:
        raise core.RiverError(f"{MUST} stays allowed: an agent reports through maxpm")
    if MUST not in new["allow"]:
        new["allow"].insert(0, MUST)
    if new == {k: of(p)[k] for k in PARTS}:
        raise core.RiverError(f"pipeline {p['name']} has this list already")
    with core.tx(conn):
        conn.execute("UPDATE pipelines SET permissions=? WHERE id=?", (json.dumps(new, separators=(",", ":")), p["id"]))
        define._change(conn, p["id"], actor, "permissions: " + "; ".join(
            f"{k}: {', '.join(new[k]) or 'nothing'}" for k in PARTS), new)
    return new


def export(p):
    """The list as the export keeps it: None for the default."""
    return None if not p["permissions"] else {k: v for k, v in of(p).items() if k in PARTS}


# ---------------------------------------------------------------- a run

def _pipeline_of(conn, run):
    return conn.execute("SELECT * FROM pipelines WHERE name=?", (run["pipeline"],)).fetchone() if run["pipeline"] \
        else None


def for_run(conn, run):
    """The run's list: the pipeline's (else the default), and the steps a yes allowed for its task alone.
    {allow, deny, ask, never, approved}."""
    p = _pipeline_of(conn, run)
    lst = of(p) if p is not None else {k: list(v) for k, v in DEFAULT.items()}
    approved = [r["rule"] for r in conn.execute("SELECT DISTINCT rule FROM pipeline_permit_asks WHERE item=? AND "
                                                "state='yes'", (run["item"],))]
    allow = list(dict.fromkeys(lst["allow"] + approved))
    deny = [x for x in lst["ask"] + lst["never"] if x not in approved]
    return {"allow": allow, "deny": deny, "ask": lst["ask"], "never": lst["never"], "approved": approved}


def prompt_text(perm, cli):
    """The lines of the run's prompt that say what it may do."""
    text = (f" What you may do here at once: {', '.join(perm['allow'])}."
            + (f" What needs a yes first: {', '.join(perm['ask'])}: it is refused here; MaximizePM turns the refused "
               "step into a card after you stop, and runs the task again after a yes." if perm["ask"] else "")
            + (f" What you never do: {', '.join(perm['never'])}." if perm["never"] else "")
            + (f" A yes allowed for this task: {', '.join(perm['approved'])}." if perm["approved"] else "")
            + " Any other step is refused too and becomes a card in the same way: do what you can without it, then "
              "release the task with a note that names the step, and stop.")
    if cli == "codex":
        text += " (Codex has no list of tools: keep to this list yourself.)"
    return text


def _input_text(tool, inp):
    """What a refused step acted on, in words: a command, a file, an address."""
    if not isinstance(inp, dict):
        return str(inp or "")[:300]
    for k in ("command", "file_path", "path", "url", "pattern"):
        if inp.get(k):
            return str(inp[k])[:300]
    return json.dumps(inp, separators=(",", ":"))[:300]


def matches(rule, tool, text):
    """Whether a permission rule covers a step: the tool's name, and for a rule with parentheses what it acts on
    (Bash: the command, ':*' at the end for each command that starts with it; another tool: a file pattern)."""
    m = re.match(r"^([A-Za-z0-9_]+)(?:\((.*)\))?$", rule)
    if not m or m.group(1) != tool:
        return False
    arg = m.group(2)
    if arg is None:
        return True
    if tool == "Bash":
        return text.startswith(arg[:-2]) if arg.endswith(":*") else text == arg
    if arg.startswith("domain:"):
        return arg[7:] in text
    return fnmatch.fnmatch(text, arg)


def exact_rule(tool, text):
    """The rule that a yes allows: that one step."""
    return f"{tool}({text})" if text else tool


def denials(path):
    """The refused steps of a Claude Code transcript (its result line's permission_denials): [(tool, text)]."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-2000:]
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("type") == "result":
            for x in d.get("permission_denials") or []:
                if isinstance(x, dict) and x.get("tool_name"):
                    step = (x["tool_name"], _input_text(x["tool_name"], x.get("tool_input")))
                    if step not in out:
                        out.append(step)
    return out


def outcome(conn, run):
    """After a run (runners._end): each refused step of the transcript becomes a card, or an audit line for a step of
    never. Returns the words for the run's result ('' when nothing was refused)."""
    if not run["transcript"]:
        return ""
    steps = denials(run["transcript"])
    if not steps:
        return ""
    p = _pipeline_of(conn, run)
    lst = of(p) if p is not None else {k: list(v) for k, v in DEFAULT.items()}
    said = []
    for tool, text in steps:
        never = next((r for r in lst["never"] if matches(r, tool, text)), None)
        rule = exact_rule(tool, text)
        if never:
            if p is not None:
                with core.tx(conn):
                    audit.write(conn, p["id"], run["agent"], "refused", f"the agent of run #{run['id']} on runner "
                                f"{run['runner']} tried {rule}, which its pipeline never allows ({never})",
                                kind="agent", place="the pipeline", result="refused",
                                detail={"run": run["id"], "item": run["item"], "rule": never})
            said.append(f"refused {rule} (never)")
            continue
        asked = next((r for r in lst["ask"] if matches(r, tool, text)), None)
        said.append(_card(conn, p, run, rule, asked))
    return "; " + "; ".join(said)


def _card(conn, p, run, rule, asked):
    """A card that asks for one step of a task: the task waits on it. Once for a task and a step."""
    old = conn.execute("SELECT * FROM pipeline_permit_asks WHERE item=? AND rule=? AND state IN ('asked','yes')",
                       (run["item"], rule)).fetchone()
    if old:
        return f"{rule} waits for a yes on #{old['card']}" if old["state"] == "asked" else f"{rule} had a yes"
    it = core._item(conn, run["item"])
    pname = p["name"] if p is not None else None
    why = f"its pipeline {pname} lists it under ask first ({asked})" if asked else \
        f"it is on no list of {'pipeline ' + pname if pname else 'its pipeline'}"
    card = core.item_add(
        conn, core._project_name(conn, it["project_id"]), f"Approve for the agent of task #{run['item']}: {rule}"[:200],
        doer="human", actor="maxpm", blocks=run["item"],  # a person's or the manager's yes (serve_pass checks who)
        context=(f"The agent of run #{run['id']} on runner {run['runner']} (task #{run['item']}: {it['title'][:120]}) "
                 f"tried this step, and the run refused it with no prompt, because {why}:\n\n    {rule}\n\n"
                 f"Approve: mark this item done (a person or the manager; an agent's done gives no yes). MaximizePM then "
                 f"runs the task again on {run['runner']} with that one step allowed for this task.\nDecline: drop "
                 f"this item; the task goes on without the step.\nThe transcript: maxpm runner log {run['id']}. To "
                 f"allow it for every agent of the pipeline: maxpm pipeline permit {pname or '<pipeline>'} --allow "
                 f"\"{rule}\"."))
    with core.tx(conn):
        conn.execute("INSERT INTO pipeline_permit_asks (pipeline_id, item, run_id, runner, rule, listed, card, state, "
                     "asked_at) VALUES (?,?,?,?,?,?,?,'asked',?)",
                     (p["id"] if p is not None else None, run["item"], run["id"], run["runner"], rule, asked,
                      card["id"], core.iso(core.now())))
        if p is not None:
            audit.write(conn, p["id"], run["agent"], "ask", f"the agent of run #{run['id']} asks for {rule}: card "
                        f"#{card['id']}", kind="agent", place="the pipeline", result="asked",
                        detail={"run": run["id"], "item": run["item"], "card": card["id"]})
    return f"{rule}: a card asks for a yes (#{card['id']})"


def serve_pass(conn):
    """One pass of maxpm serve (runners.serve_pass): a card done is a yes (the task runs again on its runner with the
    step allowed), a card dropped is a no. Returns the asks decided."""
    from .. import runners
    out = []
    for a in conn.execute("SELECT a.*, i.status card_status FROM pipeline_permit_asks a JOIN items i ON i.id=a.card "
                          "WHERE a.state='asked' AND i.status IN ('done','dropped')").fetchall():
        by = core._done_by(conn, a["card"]) if a["card_status"] == "done" else None
        # A yes is a person's or the manager's: an agent (the run's own, say) that closes the card gives none.
        yes = bool(by) and (core._is_person(conn, by) or core._is_manager(conn, by))
        if a["card_status"] == "done" and not yes:
            by = f"{by or 'someone'} (not a person or the manager: no yes)"
        with core.tx(conn):
            conn.execute("UPDATE pipeline_permit_asks SET state=?, decided_by=?, decided_at=? WHERE id=?",
                         ("yes" if yes else "no", by, core.iso(core.now()), a["id"]))
            if a["pipeline_id"]:
                audit.write(conn, a["pipeline_id"], by or "a person", "permit" if yes else "decline",
                            f"{'yes' if yes else 'no'} for {a['rule']} (task #{a['item']}, card #{a['card']})",
                            place="the pipeline", result="yes" if yes else "no",
                            detail={"item": a["item"], "card": a["card"]})
        note = ""
        if yes:
            it = conn.execute("SELECT status, assignee FROM items WHERE id=?", (a["item"],)).fetchone()
            busy = conn.execute("SELECT 1 FROM runner_runs WHERE item=? AND state IN ('queued','running')",
                                (a["item"],)).fetchone()
            if it and it["status"] not in core.CLOSED_STATES and not it["assignee"] and not busy:
                last = conn.execute("SELECT * FROM runner_runs WHERE id=?", (a["run_id"],)).fetchone()
                try:
                    note = runners.queue(conn, a["runner"], a["item"], last["pipeline"] if last else None,
                                         last["rule"] if last else None, last["rev"] if last else None)
                except core.RiverError as e:
                    note = f"; not run again: {e}"
        out.append({"ask": a["id"], "rule": a["rule"], "yes": yes, "note": note})
    return out
