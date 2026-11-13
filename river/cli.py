"""`maxpm` command line. Every command takes --json and --as <agent>."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, core
from .core import RiverError

# Agent guides: inside the package when installed (the wheel copies skills/ there), else the repository's skills/.
GUIDES = core.GUIDES

QUICKSTART = """MaximizePM (the maxpm command): a shared work queue for people and agent sessions.

Items live in projects and can wait on other items. `maxpm next` gives the
most important ready item in the area you choose; `--claim` takes it.

Agent sessions: run `maxpm go` in the project folder. It names the session,
picks a role (owner, worker, unblocker, reviewer, deployer, or idle), claims
an item, and prints a briefing. Run it again after each item. To direct the
agents and plan with the user, run `maxpm manage`.

By hand:
  maxpm register <your-name> [--human] --note "what you work on"
  export MAXPM_AGENT=<your-name>
  maxpm project list                      what each project covers; pick the one you know

Work loop:
  maxpm next --project <name> --claim     take the next item where you have context
  maxpm next --mine --claim               or: next to what you did before (after your first item)
  maxpm show <id>                         read it
  maxpm done <id> --output "what changed" finish it   (or: maxpm release <id>)

More:
  maxpm guide          how an agent works from the queue (the full loop)
  maxpm guide manager  the manager, and how to write tasks (dependencies, priorities)
  maxpm guide setup    how to set up your agents to use MaximizePM
  maxpm --help         every command
"""

# The block maxpm init puts in AGENTS.md: only what an agent needs before its first maxpm command.
# How to work (keep going, wait, report) is in the go briefing, which updates with maxpm.
AGENT_SNIPPET = """## Work queue

This project uses MaximizePM (`maxpm`) to track work and who is doing it.
When the user says "go" (or asks you to take work from the queue), run
`maxpm go` in this folder and follow the briefing it prints, to its end.
"""
# The block before #4978 (the planner role is gone): a file that holds only it still counts as only the block.
OLD_SNIPPET = AGENT_SNIPPET + 'When the user says "plan", run `maxpm plan` and follow its briefing.\n'

SETUP = """Setting up agents to use MaximizePM

1. Put the command on PATH (install.sh does it):
     ln -s <repo>/bin/maxpm ~/.local/bin/maxpm

2. Tell your agents about it. Add this block to the instructions file your
   agent reads (CLAUDE.md for Claude Code, AGENTS.md for Codex and others):

""" + "\n".join("     " + line for line in AGENT_SNIPPET.splitlines()) + """

   Or let maxpm add it:  maxpm init   (AGENTS.md holds the rules and the block;
   CLAUDE.md imports it with the line @AGENTS.md, so every agent reads one file)

3. Claude Code only, optional: install the skills so they load when needed:
     ln -s <repo>/skills/maxpm ~/.claude/skills/maxpm
     ln -s <repo>/skills/maxpm-manager ~/.claude/skills/maxpm-manager

4. Give each agent session its own name (maxpm register <name>). A person
   registers with --human and usually wants longer claims:
     maxpm config set lease_ttl 7d --agent <person>

5. Watch it:  maxpm serve --open
   Sessions that MaximizePM starts (maxpm launch, the page's Start, fresh sessions)
   open in panes of the tmux session maxpm (maxpm view shows them all in one
   terminal). Install tmux first: brew install tmux (Linux: apt install tmux).
   On Windows, MaximizePM runs only inside WSL 2: run wsl --install in
   PowerShell, then do every step here in Linux, with the project folders in
   the Linux home folder (~/...), not on /mnt/c. The page opens in the
   Windows browser at the same address; maxpm config set notify_channels
   windows shows the notices on Windows.

6. For each project, choose with the person how a finished result (a page, a
   text, a design) gets the person's look. No project gets its value in
   silence: maxpm project add, maxpm init, and maxpm project show print the
   value in force and the value to recommend. The one fact that decides: does
   something stand between a push and the users?
     push_first  the worker pushes, closes the item, and adds an item for the
       look. Recommend it when a deploy target with a review or a release
       step stands between a push and the users.
     ask_first   the worker shows the result and waits for the person's yes
       before the push. Recommend it when the project has no target, or a
       push to main is live at once (a site, a public repository). A project
       where nobody chose has this value.
   Tell the person the recommendation and its reason, then store the answer:
     maxpm project add <name> --result-look ask_first|push_first
     maxpm config set result_look ask_first|push_first --project <name>

7. Optional, the Claude desktop app: plan, manage, and answer what waits on
   you from a chat (MaximizePM runs it with no folder):
     maxpm setup-agent --claude-desktop    then quit and reopen the app
   The ChatGPT desktop app (Work and Codex modes) the same way:
     maxpm setup-agent --chatgpt-desktop   then restart the app
"""

HINTS = {
    "register": "next: export MAXPM_AGENT={name}, read maxpm project list, then maxpm next --project <name> --claim  (maxpm guide for the full loop)",
    "claim": "when finished: maxpm done {id} --output \"what changed\"   cannot finish: maxpm release {id} --note \"why\"   work found: maxpm add <project> \"title\"",
    "done": "next: maxpm next --mine --claim (work next to what you just did)  or  maxpm next --project <name> --claim",
    "release": "next: maxpm next --claim",
    "empty": "nothing ready here. Try: maxpm next (all projects), maxpm next --unblocks <id>, maxpm blockers <id>, maxpm list",
    "no_actor": "tip: maxpm register <name> and export MAXPM_AGENT=<name> so claims and history carry your name",
}


def _fmt_goal(g):
    n_open, n_done, n_back = len(g["items_open"]), len(g["items_done"]), len(g.get("items_backlog") or ())
    state = "complete" if g["status"] == "complete" else (f"owner {g['owner']}" if g["owner"] else
                                                          "shared: no owner" if g.get("shared") else "no owner")
    return (f"{g['name']} [{g['project']}] ({state}; {n_done} done, {n_open} open"
            + (f", {n_back} in backlog" if n_back else "")
            + (f"; sub-goal of {g['parent']}" if g.get("parent") else "")
            + (f"; sub-goals {', '.join(g['subgoals'])}" if g.get("subgoals") else "") + ")"
            + (f": {g['outcome']}" if g["outcome"] else ""))


def _fmt_item(a, show_reason=True):
    flags = []
    if a["status"] != "open":
        flags.append(a["status"] + (f" by {a['assignee']}" if a.get("assignee") else ""))
    elif a.get("backlog"):
        flags.append("backlog" + (f": {a['backlog_note']}" if a.get("backlog_note") else ""))
    elif a["open_blockers"]:
        flags.append("waits on " + ",".join(f"#{b}" + (" (a backlog item)" if b in (a.get("backlog_blockers") or ()) else "")
                                            for b in a["open_blockers"]))
    elif a["blocked_reason"]:
        flags.append(a["blocked_text"])
    elif a.get("busy_conflicts"):
        flags.append("conflicts with " + ",".join(f"#{b}" for b in a["busy_conflicts"]) + " (in progress)")
    else:
        flags.append("ready")
    if a["doer"] != "any":
        flags.append(a["doer"])
    if a.get("reserved_for") and a["status"] == "open":
        flags.append(f"pushed to {a['reserved_for']}" if a.get("reserved_until") else f"reserved for {a['reserved_for']}")
    if a.get("replan"):
        flags.append("replan")
    if a.get("goals"):
        flags.append("goal " + ",".join(a["goals"]))
    if a.get("refs"):
        flags.append(" ".join(r["ref"] for r in a["refs"]))
    if a.get("effective_due") and a["status"] in core.OPEN_STATES:
        flags.append(("OVERDUE " if a["due_state"] == "overdue" else "due soon " if a["due_state"] == "soon" else "due ")
                     + a["due_text"] + (f" (from #{a['due_from']})" if a.get("due_from") else ""))
    if a.get("agent"):
        flags.append(f"for {a['agent']}")
    if a.get("model") or a.get("effort") or a.get("min_model") or a.get("max_model"):
        flags.append("/".join(x for x in (a.get("model"), a.get("effort")) if x)
                     + (f" min {a['min_model']}" if a.get("min_model") else "")
                     + (f" max {a['max_model']}" if a.get("max_model") else ""))
    if a.get("same_project"):
        flags.append("same project as your earlier work")
    elif a.get("distance") is not None:
        flags.append(f"{a['distance']} link(s) away")
    line = f"#{a['id']:<4} [{a['project']}] {a['title']}  ({'; '.join(flags)})"
    if show_reason:
        line += f"\n       {a['reason']}"
    return line


def _ref_args(x):
    x.add_argument("--ref", action="append", default=[],
                   help="link an outside tracker issue: <tracker>:<key>, e.g. jira:PROJ-123, github:owner/repo#12 (repeatable)")
    x.add_argument("--ref-url", action="append", default=[], dest="ref_url",
                   help="web link of the --ref at the same position (github: refs get one by themselves)")


def _model_args(x, edit=False):
    clear = "; none clears it" if edit else ""
    x.add_argument("--model", help="recommended model for the agent that takes it (a suggestion; never blocks)" + clear)
    x.add_argument("--effort", help="recommended effort level: one of the effort_levels setting" + clear)
    x.add_argument("--min-model", dest="min_model",
                   help="weakest model allowed, one per family (opus or opus,sol); weaker sessions skip it" + clear)
    x.add_argument("--max-model", dest="max_model",
                   help="strongest model allowed, one per family (sonnet or sonnet,luna)" + clear)
    x.add_argument("--agent", help="the agent type that takes it: codex or claude-code (or a launch_agents label); "
                                   "sessions of another type skip it, and launch starts that type" + clear)


def _models(a):
    return {f: getattr(a, f) for f in core.MODEL_FIELDS if getattr(a, f) is not None}


def _model_line(a):
    """fable; effort high; min opus, sol; max fable  (a default from settings says where it comes from)."""
    parts = []
    for f, label in (("model", ""), ("effort", "effort "), ("min_model", "min "), ("max_model", "max "), ("agent", "agent ")):
        if a.get(f):
            src = a.get(f + "_from")
            parts.append(f"{label}{a[f]}" + (f" ({src})" if src == "agent_rules" or "agent_for." in (src or "") else f" ({src} default)" if src and src != "item" else ""))
    return "; ".join(parts)


def _context_lines(a, indent="  "):
    """What a new agent needs to start: why and where (context), the files (touches), how to know it works (check)."""
    out = []
    if _model_line(a):
        out.append(f"{indent}model:   {_model_line(a)}")
    if a.get("model_note"):
        out.append(f"{indent}         ({a['model_note']})")
    if a.get("context"):
        first, *rest = a["context"].splitlines() or [""]
        out.append(f"{indent}context: {first}")
        out += [f"{indent}         {line}" for line in rest]
    if a.get("touches"):
        out.append(f"{indent}touches: {', '.join(a['touches'])}")
    if a.get("check"):
        out.append(f"{indent}check:   {a['check']}")
    for r in a.get("refs", []):
        out.append(f"{indent}tracker: {r['ref']}" + (f"  {r['url']}" if r["url"] else "")
                   + ("  (updated)" if r.get("synced_at") else ""))
    for f in a.get("fed_by_detail", []):
        if f["output"]:
            out.append(f"{indent}from #{f['id']}: {f['output']}")
    return out


def _cut(text, n=70):
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _release_lines(p):
    """A target's release cadence, when its next release can start, and what waits for it (core.release_plan)."""
    name = p["target"]
    gate = p.get("approval") or {}
    if gate.get("approval") == "person":
        out = [f"release gate: a person's yes for each release (release_approval person, project {gate['project']})"]
    elif gate.get("approval") == "review":
        out = [f"release gate: a release goes out when its review passes (release_approval review, project "
               f"{gate['project']})"]
    else:
        out = [f"release gate: not set; set it from the project's needs: maxpm config set release_approval "
               f"review|person --project <deploy-{name}, or a project of the target>"]
    why = f" (why: {p['cadence_reason']})" if p.get("cadence_reason") else ""
    if not p["cadence"]:
        out += [f"release cadence: off{why} (a release starts when the one before it is done; set one: "
                f"maxpm target cadence {name} 2h|1d|1w|1mo --reason \"<the project's needs>\")"] + ([p["text"]] if p["text"] else [])
    else:
        out += [f"{p['text']}{why}; change it: maxpm target cadence {name} <time>|off --reason \"<the project's needs>\""]
    if p["last"]:
        out.append(f"  last release: {'cut' if p['last']['cut'] else 'ended'} {p['last']['text']} (deploy #{p['last']['id']})")
    ships = "; ".join(f"#{x['id']} {x['title']} ({x['status'].replace('_', ' ')})" for x in p["ships"])
    if p["waits"]:
        out.append(f"  waits for it: deploy #{p['deploy']} with {ships or 'nothing yet'}")
        out.append(f"  sooner (the target owner, the manager, or a person), with a reason: "
                   f"maxpm target release-now {name} --reason \"<why>\"")
    elif p["deploy"] and ships:
        out.append(f"  collected for the next release: deploy #{p['deploy']} with {ships}")
        out.append(f"  the cut comes when a reviewer takes its review (the deployer, with no review), or: "
                   f"maxpm target cut {name} --rev <commit>; later ship requests join the deploy item after it")
    return out


def _hook_lines(target, hooks):
    """The hooks of a target (core.target_hooks), each with its last run."""
    if not hooks:
        return [f"hooks: none (a command that MaximizePM runs on a release event: "
                f"maxpm target hook {target} {'|'.join(core.HOOK_EVENTS)} \"<command>\")"]
    out = [f"hooks (MaximizePM runs each one when its event occurs; change: maxpm target hook {target} <event> "
           f"\"<command>\" | --clear):"]
    for h in hooks:
        last = h.get("last")
        if not last:
            ran = "never ran"
        elif not last["ended_at"]:
            ran = f"last run for #{last['item_id']}: " + ("runs now" if last["started_at"] else "waits to run")
        else:
            ran = (f"last run for #{last['item_id']}: "
                   + ("stopped at hook_timeout" if last["timed_out"] else "not started" if last["exit_code"] is None
                      else f"exit {last['exit_code']}") + f", {last['ended_at']}")
        out.append(f"  {h['event']}: {h['command']}   ({ran})")
    return out


def _hooks_brief(target, hooks):
    """One line for a briefing: the hooks that MaximizePM runs by itself, so an agent does not run them by hand."""
    return (f"hooks of target {target}: MaximizePM runs " + "; ".join(f"{h['event']}: {h['command']}" for h in hooks)
            + f". Do not run these commands by hand (maxpm target hook {target})")


def _hook_result_lines(hooks):
    """What the hooks did that this command ran (core.run_hooks)."""
    out = []
    for h in hooks:
        out.append(f"hook {h['event']} of target {h['target']}: {h['result']}: {h['command']}"
                   + (f"   (in {h['folder']})" if h["folder"] else ""))
        out += ["    " + line for line in (h["output"] or "").splitlines()]
        if h.get("served"):
            out.append("    maxpm serve ran it, outside the sandbox of this session")
        elif h.get("serve_error"):
            out.append(f"    it ran in the sandbox of this session: maxpm serve did not run it ({h['serve_error']})")
        if h["ok"] is False:
            out.append(f"    the hook failed; the event stands (a hook never undoes it)"
                       + (f", and {h['alerted']} got an alert" if h["alerted"] else "")
                       + f". History: maxpm show {h['deploy']}")
            if h.get("sandbox"):
                out.append(f"    {core.HOOK_SANDBOX}.")
    return out


def _ship_line(p):
    """For the worker that asked for a ship: when its change goes out (the target's cut and release cadence)."""
    if p.get("in_cut"):
        return "a review of the release that is cut waits on this item: it goes out with that release"
    return p["text"] + (f"; your change goes out with the next release (deploy #{p['deploy']}). The target owner or "
                        f"the manager decides a release sooner (maxpm target release-now {p['target']} --reason "
                        f"\"<why>\")" if p["waits"] else "")


def _match_state(m):
    """open | in progress by x | done 2026-10-12 | backlog: the state of a search or similar-item match."""
    st = m["status"].replace("_", " ")
    if m.get("assignee") and m["status"] in ("in_progress", "held"):
        st += f" by {m['assignee']}"
    if m["status"] in core.CLOSED_STATES and m.get("closed_at"):
        st += f" {m['closed_at'][:10]}"
    return st


def _related_lines(rows, indent="  "):
    out = []
    for m in rows:
        out.append(f"{indent}  #{m['id']} [{_match_state(m)}] {_cut(m['title'], 90)}  (score {m['score']:.2f})")
        if m.get("output"):
            out.append(f"{indent}      output: {_cut(' '.join(m['output'].split()), 160)}")
    return out


def render_search(res, limit):
    q = res["query"]
    if res["items"]:
        print(f"{res['total']} item(s) with every word of \"{q}\", newest first:")
        for m in res["items"]:
            print(f"  #{m['id']:<5} [{_match_state(m)}{', archived' if m.get('archived') else ''}] [{m['project']}] "
                  f"{m['title']}")
            if m.get("output"):
                print(f"         output: {_cut(' '.join(m['output'].split()), 160)}")
        if res["total"] > len(res["items"]):
            print(f"... {res['total'] - len(res['items'])} more (-n {res['total']}, or more words)")
        return
    if res["closest"]:
        print(f"No item has every word of \"{q}\". The closest:")
        for m in res["closest"]:
            print(f"  #{m['id']:<5} [{_match_state(m)}] [{m['project']}] {m['title']}  (score {m['score']:.2f})")
        return
    print(f"No item like \"{q}\".")


def render_duplicates(groups):
    if not groups:
        print("No open items look like the same work (similar_min).")
        return
    print(f"{len(groups)} group(s) of open items that look like the same work:")
    for g in groups:
        print(f"{g['project']} (score {g['score']:.2f}):")
        for m in g["items"]:
            print(f"  #{m['id']:<5} [{_match_state(m)}] {m['title']}" + ("  (a person's)" if m["doer"] == "human" else ""))
    print("Merge the same work into the item that stays: maxpm merge <keep-id> <other-id> --note \"<why the same>\"")
    print("Not the same work: link them (maxpm dep <id> --on <id>), or leave them.")


def _print_show(a):
    print(_fmt_item(a))
    if a.get("short"):
        print("  short:", a["short"])
    if a.get("kind") == "deploy":
        print(f"  deploy item for target {a['target']} (owner: {a.get('target_owner') or 'nobody'}; only the owner takes it)")
    if a.get("kind") == "deploy" and a.get("cut_at"):
        print(f"  release cut {a['cut_at'][:16].replace('T', ' ')} UTC: it holds exactly these items; a later ship request "
              f"joins the next deploy item")
    if (a.get("release") or {}).get("text") or (a.get("release") or {}).get("in_cut"):  # maxpm ship
        print(f"  {_ship_line(a['release'])}")
    if a.get("ship_warning"):  # maxpm ship of an item that is not done (#5386)
        print(f"  WARNING: {a['ship_warning']}")
    if a["notes"]:
        print("  notes:", a["notes"])
    for line in _context_lines(a):
        print(line)
    if a.get("kind") == "deploy" and a["waits_on_detail"]:
        print("  ships:", ", ".join(f"#{d['id']} {d['title']} ({d['status']})" for d in a["waits_on_detail"]))
    elif a.get("kind") == "review" and a["waits_on_detail"]:
        print("  reviews:", ", ".join(f"#{d['id']} {d['title']} ({d['status']})" for d in a["waits_on_detail"]))
    if a.get("fixes"):
        print("  sent back with fixes:", ", ".join(f"#{f['id']} {f['title']} ({f['project']})" for f in a["fixes"]))
    if a.get("asked"):
        print(f"  blocked; the user decides first: #{a['asked']['id']} {a['asked']['title']} (in Needs you). "
              "Done adds the fixes still in its list; drop adds none.")
    if a.get("fixes_added"):
        print("  approved fixes added (the release review waits on them):",
              ", ".join(f"#{f['id']} {f['title']}" for f in a["fixes_added"]))
    elif a["waits_on_detail"]:
        print("  waits on:", ", ".join(f"#{d['id']} {d['title']} ({d['status']}"
                                       + ("; feeds: you read its output" if d.get("kind") == "feeds" else "") + ")"
                                       for d in a["waits_on_detail"]))
    if a["unblocks_detail"]:
        print("  unblocks:", ", ".join(f"#{d['id']} {d['title']} ({d['status']})" for d in a["unblocks_detail"]))
    if a.get("conflicts_detail"):
        print("  conflicts with (never in progress together):", ", ".join(
            f"#{d['id']} {d['title']} ({d['status']}" + (f" by {d['assignee']}" if d["assignee"] else "") + ")"
            for d in a["conflicts_detail"]))
    if a.get("lease_expires_at"):
        print("  lease until:", a["lease_expires_at"])
    if a.get("hold_expires_at"):
        print("  held until:", a["hold_expires_at"], "(renewed by your commands; maxpm release ends it)")
    if a.get("holder_wait"):
        w = a["holder_wait"]
        left = core._short(core.parse_iso(w["until"]) - core.now())
        print(f"  you wait for this person at most {w['max']} (human_wait_max; {left} left). Then MaximizePM releases "
              f"#{w['item']}, which still waits on this item, and you take other work: maxpm go")
    if a.get("mine"):
        print(f"  reserved for you: no other session takes it while your session lives. When it is done: "
              f"maxpm done {a['id']} --output \"...\"   (to work on it alone first: maxpm claim {a['id']})")
    if a.get("changed"):
        print("  changed:", a["changed"])
    if a.get("output"):
        print("  output:", a["output"])
    if a.get("found_during"):
        print(f"  found during: #{a['found_during']}")
    if a.get("found_here"):
        print("  found while doing this:", ", ".join(f"#{d['id']} {d['title']} ({d['status']})" for d in a["found_here"]))
    if a.get("message_count"):
        print(f"  messages: {a['message_count']} (maxpm thread --item {a['id']})")
    if a.get("artifacts"):
        x = a["artifacts"]
        print(f"  artifacts: {x['count']}" + (f" of #{x['item']}, where it was found" if x["item"] != a["id"] else "")
              + f", maxpm artifacts --item {x['item']}")
    if a.get("related"):
        print("  related (work like it, not linked; maxpm show <id>):")
        print("\n".join(_related_lines(a["related"])))
    if a.get("usage"):
        print(f"  usage:   {_usage_line(a['usage'])}")
    if a.get("shipped_in"):
        print(f"  ship requested: joins deploy item #{a['shipped_in']}")
        if (a.get("ship_release") or {}).get("text") or (a.get("ship_release") or {}).get("in_cut"):
            print(f"  {_ship_line(a['ship_release'])}")
    if a.get("now_ready"):
        print("  now ready:", ", ".join(f"#{i}" for i in a["now_ready"]))
    if a.get("resumed"):
        print("  back in progress for its holder:", ", ".join(f"#{i}" for i in a["resumed"]))
    if a.get("close_check"):
        print("\n".join(_close_check_lines(a["close_check"], a["id"] if "merged" not in a else a["merged"]["id"])))
    if a.get("goal_checks"):
        print("\n".join(_goal_check_lines(a["goal_checks"])))
    for g in a.get("shared_handoffs") or []:
        print(f"  goal {g} is shared, so you keep its handoff now: maxpm goal handoff {g} --file <path> (what this "
              f"item changed, the decisions, the files that matter, what is left). The next session on the goal reads it.")
    for e in a.get("events", [])[:8]:
        print(f"  {e['at']} {e['actor']}: {e['change']}")


def _goal_check_lines(rows):
    """The reply of done, drop, and goal done when a goal has no open item now (#4970): who decides whether
    its done-when holds."""
    out = []
    for x in rows:
        test = f" ({_cut(x['done_when'], 120)})" if x["done_when"] else ""
        if x["asked"] == "you":
            out.append(f"  GOAL {x['goal']} has no open item now: you own it. Does its done-when hold{test}? Yes: "
                       f"maxpm goal done {x['goal']} --result \"<one line>\""
                       + (" --drop-open (the backlog items " + ", ".join(f"#{i}" for i in x["backlog"]) + " stay)"
                          if x["backlog"] else "")
                       + f". No: add the items it still needs (maxpm add \"<title>\" --goal {x['goal']})")
        elif x["asked"] == "manager":
            out.append(f"  GOAL {x['goal']} has no open item now and no owner: the manager decides whether its "
                       f"done-when holds{test} (maxpm manage lists it)")
        else:
            out.append(f"  GOAL {x['goal']} has no open item now: MaximizePM asked its owner {x['asked']} whether its "
                       f"done-when holds")
    return out


def _close_check_lines(rows, closed, r="maxpm"):
    """The close check in the reply of done, drop, and merge (#3286): decide each item now."""
    out = [f"  MAY BE UNNECESSARY NOW (the close of #{closed}; decide each one now):"]
    for x in rows:
        out.append(f"    #{x['id']} {_cut(x['title'], 70)}" + ("  [a person's item]" if x["doer"] == "human" else "")
                   + (f"  [held by {x['assignee']}]" if x["assignee"] and x["status"] != "open" else "")
                   + ("  [NOW READY]" if x.get("ready") else "") + f"  ({x['why']})")
    out.append(f"  Not needed: {r} done <id> --note \"<why>\"  |  {r} drop <id> --note \"<why>\"  |  "
               f"the same work: {r} merge <keep> <id> --note \"<why>\"")
    if any(x.get("ready") for x in rows):
        out.append(f"  NOW READY only because of this close; its real precondition may still be false. Make it "
                   f"wait again: {r} dep <id> --on <what it waits on>; or it is ready: {r} keep <id> --close "
                   f"{closed} --note \"ready: <why>\"")
    out.append(f"  Still needed: {r} keep <id> --close {closed} --note \"<why>\"   (all of them: {r} keep --close {closed})")
    return out


def _print_brief(a, n=240):
    """maxpm show --brief: what an agent needs to decide on an item, in a few lines (no history, no closed
    links, long text cut). maxpm show prints all of it."""
    flat = lambda t: " ".join(t.split())
    print(_fmt_item(a, show_reason=False))
    for label, text in (("context", a.get("context")), ("notes", a.get("notes")), ("output", a.get("output"))):
        if text and text.strip():
            print(f"  {label}: {_cut(flat(text), n)}")
    if a.get("touches"):
        print(f"  touches: {_cut(', '.join(a['touches']), n)}")
    if a.get("check"):
        print(f"  check:   {a['check']}")
    for label, key in (("waits on", "waits_on_detail"), ("unblocks", "unblocks_detail")):
        live = [d for d in a.get(key) or [] if d["status"] in core.OPEN_STATES]
        if live:
            print(f"  {label}: " + ", ".join(f"#{d['id']} {_cut(d['title'], 40)} ({d['status']})" for d in live[:3])
                  + (f" and {len(live) - 3} more" if len(live) > 3 else ""))
    if a.get("lease_expires_at"):
        print("  lease until:", a["lease_expires_at"])
    if a.get("message_count"):
        print(f"  messages: {a['message_count']} (maxpm thread --item {a['id']})")
    if a.get("artifacts"):
        x = a["artifacts"]
        print(f"  artifacts: {x['count']}" + (f" of #{x['item']}, where it was found" if x["item"] != a["id"] else "")
              + f", maxpm artifacts --item {x['item']}")
    if a.get("related"):
        print("  related: " + ", ".join(f"#{m['id']} {_cut(m['title'], 40)} ({_match_state(m)})" for m in a["related"]))
    print(f"  all of it: maxpm show {a['id']}")


def _k(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(round(n))


def _usage_line(u):
    """2 session(s) (fork+fresh), 48 turns: 732k eq in (read 5.1M, write 210k), 31k out, peak context 180k"""
    return (f"{u['sessions']} session(s) ({u['started']}), {u['turns']} turns: {_k(u['eq'])} eq in "
            f"(read {_k(u['cache_read'])}, write {_k(u['write_1h'] + u['write_5m'])}), {_k(u['output'])} out, "
            f"peak context {_k(u['peak_context'])}")


def render_usage(res, limit=20):
    if not res["items"]:
        print(f"(no measured items in {res['since']}: MaximizePM measures an item when it is done, from the "
              f"transcripts of the Claude Code sessions that held it)")
        return
    print("eq = input + 0.1 x cache read + 2 x 1h cache write + 1.25 x 5m cache write (input-token equivalents)")
    for g in res["groups"]:
        print(f"  {g['started']:<11} {g['items']:>4} item(s): median {_k(g['median_eq'])} eq, "
              f"{g['median_turns']:.0f} turns, {_k(g['median_output'])} out; {_k(g['eq_per_turn'])} eq per turn")
    print()
    for it in res["items"][:limit]:
        print(f"  #{it['id']:<5} {_k(it['eq']):>6} eq {_k(it['output']):>5} out {it['turns']:>4} turns  "
              f"{it['started']:<10} [{it['project']}] {_cut(it['title'], 50)}")
    if len(res["items"]) > limit:
        print(f"  ... {len(res['items']) - limit} more (--limit {len(res['items'])})")


def _num(v, fmt="{:.1f}", none="-"):
    return none if v is None else fmt.format(v)


def render_health(res):
    """maxpm health (#5353): the headline first, then each measure with the value of the time before."""
    n, b = res["now"], res["before"]

    def pair(key, fmt="{:.1f}", unit=""):
        return f"{_num(n[key], fmt + unit)}  (before {_num(b[key], fmt + unit)})"
    days = f"{n['days']:g}d"
    print(f"Does MaximizePM work?{' Project ' + res['project'] + ',' if res['project'] else ''} "
          f"{n['since'][:16]} to {n['until'][:16]} UTC ({days}), against the {days} before")
    print(f"HEADLINE  person minutes per done item: {pair('person_minutes_per_item', '{:.2f}')}")
    print(f"          a proxy: {res['person_minutes']} min per needs-you row (health_person_minutes), not measured time")
    print(f"  items done per day   {pair('per_day')}   {n['done']} done")
    print(f"  rework rate          {pair('rework_rate', '{:.1%}')}   reopens {n['reopens']}, lost claims "
          f"(lease ran out) {n['lost_claims']}, review fixes {n['review_fixes']}")
    print(f"  person wait          mean {pair('wait_mean_h', '{:.1f}', 'h')}, p90 {_num(n['wait_p90_h'], '{:.1f}h')} "
          f"({n['wait_closed']} closed); open at the end {n['open_now']}, mean age {_num(n['open_mean_age_h'], '{:.1f}h')}")
    print(f"  person load          {pair('person_load')} needs-you rows per 100 done items "
          f"({n['needs_you_opened']} opened)")
    s = n["sessions"]
    if s["total_secs"]:
        names = {"waiting_work": "waiting for work", "waiting_person": "waiting on a person"}
        parts = ", ".join(f"{names.get(k, k)} {v:.0%}" for k, v in s["share"].items() if v)
        print(f"  session idle         {s['idle']:.0%} waiting for work  (before "
              f"{_num(b['sessions']['idle'] if b['sessions']['total_secs'] else None, '{:.0%}')}); "
              f"{parts}; {s['sessions']} sessions, {s['total_secs'] / 3600:.1f}h sampled")
    else:
        print(f"  session idle         no samples in this time (maxpm serve records them from "
              f"{(s['first'] or 'its next start')[:16]})")
    if n["cost_measured"]:
        kinds = ", ".join(f"{k} {_k(v['median_eq'])} ({v['items']})" for k, v in n["cost_by_kind"].items())
        print(f"  cost per done item   median {_k(n['cost_median_eq'])} eq  (before "
              f"{_k(b['cost_median_eq']) if b['cost_median_eq'] is not None else '-'}), mean {_k(n['cost_mean_eq'])}; "
              f"{n['cost_measured']} of {n['done']} measured; by kind: {kinds}")
    else:
        print("  cost per done item   no measured items (maxpm usage)")
    if res.get("changes"):
        print("CHANGES in this time, before -> after (weak proof: other things change at the same time)")
        for ch in res["changes"]:
            bf, af = ch["before"] or {}, ch["after"] or {}

            def ba(key, fmt):
                return f"{_num(bf.get(key), fmt)} -> {_num(af.get(key), fmt)}"
            print(f"  {ch['at'][:10]} {ch['name']}: person min/item {ba('person_minutes_per_item', '{:.2f}')}, "
                  f"done/day {ba('per_day', '{:.1f}')}, rework {ba('rework_rate', '{:.1%}')}  "
                  f"({_num(bf.get('days'), '{:g}d')} before, {_num(af.get('days'), '{:g}d')} after)")
    if res.get("by_goal"):
        _render_goal_split(res["by_goal"])
    elif len(n["by_project"]) > 1:
        top = sorted(n["by_project"].items(), key=lambda kv: -kv[1])
        print("  done by project      " + ", ".join(f"{p} {c}" for p, c in top[:8])
              + (f", {len(top) - 8} more" if len(top) > 8 else ""))


def _render_goal_split(g):
    """maxpm health --by goal (#5355): goal against no goal, the weighted total first, then each project."""
    def line(label, x):
        small = f"  too few to read (under {g['min_group']})" if x["small"] else ""
        return (f"  {label:<9} {x['done']:>5} done  {_num(x['median_minutes'], '{:.0f}'):>5} min claim to done  "
                f"rework {_num(x['rework_rate'], '{:.1%}'):>6}  person load {_num(x['person_load'], '{:.0f}'):>4}  "
                f"cost {_k(x['median_eq']) if x['median_eq'] is not None else '-':>6} eq{small}")
    print("BY GOAL: agent task items with a goal tag against items without one, inside the projects that have both;")
    print("  in the total each project weighs as its smaller group (a tag counts as it is now)")
    if not g["projects"]:
        print("  no project has done items of both kinds in this time")
        return
    print(line("goal", g["total"]["goal"]))
    print(line("no goal", g["total"]["none"]))
    for p in g["projects"]:
        print(f"  [{p['project']}]")
        print("  " + line("goal", p["goal"]))
        print("  " + line("no goal", p["none"]))
    lo = g["left_out"]
    if lo["goal"] or lo["none"]:
        print(f"  left out (their project has one kind only): {lo['goal']} with a goal, {lo['none']} without")


def _handoff_lines(goal, h, due=None, indent="  "):
    """The handoff of a goal for goal show and the go briefing; and a line when its owner should renew it."""
    out = []
    if h:
        out.append(f"{indent}HANDOFF v{h['version']} ({h['by_agent'] or '?'}, {h['created_at']}):")
        out += [f"{indent}  {line}" if line else "" for line in (h.get("text") or "").splitlines()]
    else:
        out.append(f"{indent}handoff: none yet (maxpm goal handoff {goal} --file <path>)")
    if due and due.get("shared"):
        out.append(f"{indent}The handoff is older than #{due['id']} ({_cut(due['title'], 50)}), finished at "
                   f"{due['closed_at']}: read its output too, and bring the handoff up to date (maxpm goal handoff "
                   f"{goal} --file <path>).")
    elif due:
        out.append(f"{indent}The handoff is older than #{due['id']}, which its owner finished at {due['closed_at']}: "
                   f"renew it (maxpm goal handoff {goal} --file <path>) before goal release or give.")
    return out


def render_handoff(res, a):
    if a.versions:
        if not res["versions"]:
            print(f"(goal {res['goal']} has no handoff yet)")
        for v in res["versions"]:
            print(f"  v{v['version']}  {v['created_at']}  {v['by_agent'] or '?'}  {v['chars']} characters")
        return
    if a.text is not None or a.file:
        h = res["handoff"]
        print(f"goal {res['goal']}: handoff v{h['version']} stored ({len(h['text'])} characters). "
              f"Sessions that start on its items read it.")
        return
    print("\n".join(_handoff_lines(res["goal"], res["handoff"], None if a.version else res["due"], indent="")))


def _print_tree(n, prefix="", last=True, root=True):
    left = f", {n['lease_seconds_left'] // 60}m left" if n["lease_seconds_left"] is not None else ""
    who = f", {n['assignee']}{left}" if n["assignee"] else ""
    state = "ready" if n["ready"] else n["status"]
    if n.get("backlog"):
        state = f"a backlog item: {n['backlog_note']}; maxpm activate {n['id']} makes it open again"
    if n["blocked_reason"]:
        state += ", " + n["blocked_text"]
    for c in n.get("busy_conflicts", []):
        state += f", conflicts with #{c['id']} held by {c['assignee']}"
    label = f"#{n['id']} {n['title']}  ({state}{who})"
    if root:
        print(label)
    else:
        print(prefix + ("└─ " if last else "├─ ") + label)
    kids = n["children"]
    for i, c in enumerate(kids):
        ext = "" if root else ("   " if last else "│  ")
        _print_tree(c, prefix + ext, i == len(kids) - 1, False)


def _unread_text(u, actor):
    if not u["unread"] and not u["questions"]:
        return None
    parts = [f"{u['unread']} unread"] if u["unread"] else []
    if u["alerts"]:
        parts.append(f"{u['alerts']} alert{'s' if u['alerts'] > 1 else ''}")
    if u["questions"]:
        parts.append(f"{u['questions']} question{'s' if u['questions'] > 1 else ''} to answer"
                     + (f", {u['questions_waiting']} waiting over {u['nudge_after']}" if u.get("questions_waiting") else ""))
    return f"inbox: {', '.join(parts)} (maxpm --as {actor} inbox)"


def _footer(conn, actor):
    if not actor:
        return
    r = conn.execute("SELECT 1 FROM agents WHERE name=?", (actor,)).fetchone()
    if not r:
        print(f"(agent {actor} is not registered: maxpm register {actor} [--human])", file=sys.stderr)
        return
    st = core.agent_status(conn, actor)
    holds = st["holds"]
    bits = []
    if holds:
        parts = []
        for h in holds:
            if h["lease_expires_at"]:
                mins = int((core.parse_iso(h["lease_expires_at"]) - core.now()).total_seconds() // 60)
                parts.append(f"#{h['id']} {mins}m left")
            elif h.get("hold_expires_at"):
                parts.append(f"#{h['id']} held {core._short(core.parse_iso(h['hold_expires_at']) - core.now())} left")
            else:
                parts.append(f"#{h['id']}")
        bits.append(f"holds {', '.join(parts)}")
    if st["owns"]:
        bits.append("owns " + ", ".join(
            f"{o['name']} {core._short(core.parse_iso(o['owner_expires_at']) - core.now())} left" for o in st["owns"]))
    msg = _unread_text(core.unread(conn, actor), actor)
    if msg:
        bits.append(msg)
    asked = core.asked_open(conn, actor)
    if asked:  # #5001: the asker closes each question that is answered or moot
        bits.append(f"{len(asked)} open question(s) you asked ({', '.join('#' + str(q['id']) for q in asked)}): close "
                    f"each answered one: maxpm --as {actor} answer <id> --self \"...\", or a moot one: withdraw <id> "
                    f"--note \"...\"")
    gap = core.watch_gap(conn, actor)
    if gap is not None:  # a manager whose watch ended: nothing wakes it at its prompt (#1898)
        bits.append(f"no watch runs for {core._short(gap)}: start maxpm --as {actor} manage --watch in the "
                    f"background before you end your turn")
    if bits:
        print(f"[{actor}: {'; '.join(bits)}]", file=sys.stderr)


def _fmt_msg(m, indent="", states=False):
    """One message for a reader. states: the sender's view (maxpm thread), with where the message is: delivered,
    read, answered (#5685)."""
    to = m["to_agent"] or (f"the holder of #{m['item_id']}" if m["item_id"] else "?")
    about = f" about #{m['item_id']}" + (f" {m['item_title']}" if m.get("item_title") else "") if m["item_id"] else ""
    by = f" for {m['for_person']}" if m.get("for_person") else ""  # the manager answered for the person (#5307)
    head = f"{indent}#{m['id']} {m['kind']} from {m['from_agent']}{by} to {to}{about}  ({m['created_at']}"
    if m["kind"] in ("question", "offer") or m["state"] not in ("open", "read"):
        head += f", {m['state']}"
    if m.get("level_set"):
        head += f", level {m['level']}"
    if m.get("blocked"):  # with what MaximizePM saw, when the sender did not set the flag
        head += {"waits": ", blocked (the sender waits for messages)",
                 "item": ", blocked (the sender released or blocked its item)"}.get(m["blocked"], ", blocked")
    if m.get("held_for"):
        head += f", held for the manager {m['held_for']}"
    if states:
        head += ", " + core.delivery_text(m)
    elif m["unread"]:
        head += ", new"
    if m["reply_to"]:
        head += f", reply to #{m['reply_to']}"
    lines = [head + ")"]
    lines += [f"{indent}    {line}" for line in m["body"].splitlines() or [""]]
    return "\n".join(lines)


LEVEL_HELP = ("to the manager: how long it waits before it wakes the manager's watch: high (the default; "
              "manage_wait_high, 10m) or low (manage_wait_low, 30m); with --blocked it does not wait")
BLOCKED_HELP = ("your item cannot move until the answer: you have no other part of it to do, and nobody else can do "
                "the next step; it wakes the manager's watch at once")


class _Null:
    """Stands in for the parser of a command that does not run: every call on it does nothing."""

    def __getattr__(self, name):
        return self

    def __call__(self, *args, **kwargs):
        return self


class _OneCommand(argparse._SubParsersAction):
    """The commands, of which only `only` gets its parser: the others are names. Building all of them took 25 ms
    of each maxpm command (#5491)."""
    only = None

    def add_parser(self, name, **kwargs):
        if self.only is not None and name != self.only:
            self.choices[name] = None
            return _Null()
        return super().add_parser(name, **kwargs)


class _Retry(Exception):
    """The parser of one command could not read the words: the whole parser reads them again."""


class _QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise _Retry(message)


def _command_word(words):
    """The command in the words (after the global options), or None when they ask for help or the version."""
    it = iter(words)
    for w in it:
        if w in ("-h", "--help", "--version"):
            return None
        if w == "--as":
            next(it, None)
        elif not w.startswith("-"):
            return w if not {"-h", "--help"} & set(words) else None
    return None


def parse_args(argv=None):
    """The parsed words: with the parser of their command alone, else (help, a mistake) with the whole one."""
    words = sys.argv[1:] if argv is None else list(argv)
    cmd = _command_word(words)
    if cmd:
        try:
            args = build_parser(only=cmd).parse_args(words)
            if args.cmd == cmd:
                return args
        except _Retry:
            pass
    return build_parser().parse_args(words)


BROWSER_WAITS = ("auto", "load", "idle", "none")  # browser.WAITS, without the import of browser at each command
# #5683: the rules of the browser that the skill names only in short; maxpm browser -h prints them above the commands.
BROWSER_GUIDE = """\
Each project has one browser (headless Google Chrome, its own profile) that maxpm serve runs. The person sees and
uses it in the side panel of your session; while it shows, it draws your pointer and the keys you type.

Read small views, not HTML:
  open <address>        replies with the page's outline (regions, counts, headings)
  snapshot              the elements in the window, one line each with a ref (@e3); repeated rows fold
  find <words>          elements anywhere on the page by name, row or column
  text                  the main text, about 4 KB; --section, --grep, --from for more
  When the names do not work: html --scope <ref>, then html, then screenshot <target> --grid and --at.

Act by name: click "Comments", click link "Comments", fill "Month" 2026-10,
click "Delete" --in "Invoice 1042". Two matches: nothing is done and the reply lists them. A ref (@e3) and a CSS
selector work too. The reply says what changed (a new page's outline, a menu that opened, the page's errors).

Control: one controller at a time. control takes it for your session, give <label> says that your subagent drives
it, release gives it up. The person can hand it to you (Hand to in the panel): your chat then gets its address.
While the person holds it (Take control), commands that change the page are refused; reads work. control --take
takes it from the person only when they asked you in your chat, or after browser_take_after (2m) with no input
from them; a Playwright click takes it the same way. With the Playwright tools, a refused click, typing or
browser_evaluate fails as a timeout or "Execution context was destroyed".

The browser opens no loopback or private address (your dev server): the person allows one for the project with
maxpm config set browser_allow "localhost:5173" --project <name>.
Only the person signs in: never type a password. Never solve a CAPTCHA: ask the person. A site that refuses a
headless browser: the person presses Open as a window (run window only when they asked in your chat).
The person's marked-up pages come to your chat as [maxpm annotations a<id> ...] with the commands to answer them."""
BROWSER_ROLES = {"link", "button", "textbox", "searchbox", "checkbox", "radio", "switch", "combobox", "listbox", "slider",
                 "spinbutton", "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "option", "treeitem", "file",
                 "image",
                 "clickable"}  # browser.ROLES: click link "Comments" (#5667)


def build_parser(only=None):
    """The parser of every command; only: the name of the one command that gets its arguments (parse_args)."""
    p = (_QuietParser if only else argparse.ArgumentParser)(
        prog=core.COMMAND, description="MaximizePM: a dependency-ordered work queue for agents and people.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--as", dest="actor", default=os.environ.get("MAXPM_AGENT"), help="agent name (default $MAXPM_AGENT)")
    p.add_argument("--quiet", "-q", action="store_true", default=bool(os.environ.get("MAXPM_QUIET")),
                   help="no hint lines")
    sub = p.add_subparsers(dest="cmd", action=_OneCommand)
    sub.only = only

    pr = sub.add_parser("project", help="add, rank, list, or archive projects")
    prs = pr.add_subparsers(dest="pcmd", required=True)
    x = prs.add_parser("add"); x.add_argument("name"); x.add_argument("--rank", type=int)
    x.add_argument("--description", "--notes", dest="notes", default="",
                   help="what the project covers and what context helps (agents read this to pick an area)")
    x.add_argument("--path", help="folder this project lives in; maxpm go run there finds it")
    x.add_argument("--target", help="deploy target this project ships to (maxpm target list)")
    x.add_argument("--result-look", choices=("ask_first", "push_first"),
                   help="the look of the person at a finished result, as the person confirmed it: ask_first (the worker "
                        "waits for the yes before the push; a project where nobody chose has it) or push_first (the "
                        "worker pushes and adds an item for the look)")
    x = prs.add_parser("describe", help="set a project's description"); x.add_argument("name"); x.add_argument("text")
    x = prs.add_parser("path", help="link a project to a folder (maxpm go uses it)"); x.add_argument("name"); x.add_argument("path", nargs="?")
    x.add_argument("--move", action="store_true", help="move a project that is linked to another folder (the user decides)")
    x = prs.add_parser("show", help="a project's description, who works on it, and its ready items"); x.add_argument("name")
    x = prs.add_parser("target", help="put a project in a deploy target (no target clears it)")
    x.add_argument("name"); x.add_argument("target", nargs="?")
    x = prs.add_parser("tracker", help="the outside tracker a project uses (agents import from it and update it)")
    x.add_argument("name"); x.add_argument("text", nargs="?",
                                          help="which tracker, where, what tool: \"github owner/repo via gh\"; none clears it")
    x = prs.add_parser("rank"); x.add_argument("name"); x.add_argument("rank", type=int)
    x = prs.add_parser("rename", help="give a project a new name; the old name stops at once")
    x.add_argument("name"); x.add_argument("new")
    x = prs.add_parser("archive", help="archive a project: its items leave the queue and the lists; nothing is deleted")
    x.add_argument("name")
    x = prs.add_parser("restore", aliases=["unarchive"], help="bring an archived project back, with its items")
    x.add_argument("name")
    x = prs.add_parser("list"); x.add_argument("--archived", action="store_true", help="the archived projects")

    br = sub.add_parser("browser", help="the browser of a project (#4631): the person sees and uses it in the side panel of "
                        "the Sessions tab; a session drives it with these commands (one controller at a time)",
                        description=BROWSER_GUIDE, formatter_class=argparse.RawDescriptionHelpFormatter)
    brs = br.add_subparsers(dest="bcmd", required=True)
    bp = argparse.ArgumentParser(add_help=False)
    bp.add_argument("--project", help="default: the project of the item you hold, or of this folder")
    bp.add_argument("--tab", help="the tab (its id from maxpm browser status); default: the tab that shows")
    # #5663: what an action waits for, and the snapshot after it
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--wait", choices=BROWSER_WAITS, help="auto (default): for the next page when the action led to "
                    "one; load: for the next page; idle: also until no request is in flight; none: not at all")
    ap.add_argument("--snapshot", action="store_true", help="print the snapshot (-i) of the page after the action")
    x = brs.add_parser("status", parents=[bp], help="whether it runs, its tabs, and who controls it")
    x = brs.add_parser("snapshot", parents=[bp], help="the page's interactive elements in the window, one line each "
                       "with a ref (@e3) that click, type, fill, select and check take (repeated rows: one line each); "
                       "with its headings unless -i")
    x.add_argument("-i", "--interactive", action="store_true", help="only the interactive elements")
    x.add_argument("--all", action="store_true", help="the whole page, not only what shows in the window")
    x.add_argument("--region", help="one region: header, nav, main, footer, aside, dialog, search, menu, or its label "
                   "or heading")
    x.add_argument("--scope", help="only the part of the page in this CSS selector's first element")
    x.add_argument("--delta", action="store_true", help="only what changed since the last snapshot of this page")
    x.add_argument("--max", type=int, help="the elements it lists at most (default 400)")
    x = brs.add_parser("find", parents=[bp], help="the elements anywhere on the page whose name, row or column holds "
                       "these words, with refs")
    x.add_argument("words", nargs="+")
    x = brs.add_parser("open", parents=[bp, ap], help="open an address in the tab the panel shows (and show the "
                       "browser in your side panel); the reply is the page's outline")
    x.add_argument("url"); x.add_argument("--no-show", action="store_true", help="do not open it in your side panel")
    x.add_argument("--no-outline", action="store_true", help="reply with the title and address only")
    x.add_argument("--text", action="store_true", help="also the main text, as maxpm browser text gives it")
    x = brs.add_parser("outline", parents=[bp], help="the page's regions with their counts of links, buttons, fields "
                       "and rows, and its headings (about 0.5 KB)")
    x = brs.add_parser("extract", parents=[bp], help="rows with named fields as JSON: extract --rows @e12 "
                       "title='.titleline a' link='.titleline a@href' (text, or @attribute); a missing field is null")
    x.add_argument("fields", nargs="*", metavar="name=<css>[@attr]", help="the fields of each row (default: its text)")
    x.add_argument("--rows", help="the rows: a ref or name of an element in a repeated row (its rows, as the snapshot "
                   "folds them), or a CSS selector; none: the page as one row")
    x.add_argument("--limit", type=int, help="the rows it gives at most (default 100)")
    x = brs.add_parser("screenshot", parents=[bp], help="a PNG of the tab, or of one element (about 1000 pixels on its "
                       "long side at most); prints the file's path")
    x.add_argument("target", nargs="?", help="only this element: its name, role and name, or @e5 (a map, a chart, a "
                   "canvas, an element with no usable name)")
    x.add_argument("--grid", action="store_true", help="with a target: light lines at each 10%% of its box, with "
                   "labels, to name a point for --at")
    x.add_argument("--out", help="the file to write (default: a new file in the temporary folder)")
    x.add_argument("--full", action="store_true", help="the whole page, not only what shows")
    x.add_argument("--cursor", action="store_true", help="draw the driving session's pointer and its last click ring in")
    x = brs.add_parser("html", parents=[bp], help="the HTML of the page (the DOM as it is now)")
    x.add_argument("--out", help="write it to this file")
    x.add_argument("--scope", help="only one element: a ref (@e3) or a CSS selector")
    x = brs.add_parser("text", parents=[bp], help="the main text of the page as a person reads it, without its menus "
                       "and footers, up to 4 KB, and how much more there is")
    x.add_argument("--section", help="only the section under this heading (maxpm browser outline lists them)")
    x.add_argument("--grep", help="only the lines with this word, and one line around each")
    x.add_argument("--from", dest="start", type=int, help="from this character on (the more line names it)")
    x.add_argument("--max", type=int, help="the bytes it prints at most (default 3800)")
    x.add_argument("--all", action="store_true", help="all the text of the page, menus too, with no limit")
    # #5667: an element by its name as a snapshot prints it, or by role and name, in its row with --in
    ep = argparse.ArgumentParser(add_help=False)
    ep.add_argument("--in", dest="within", metavar="TEXT", help="only in the row, card or region whose text holds this")
    # #5689: a point inside the target's box: a map, a chart, a canvas, an editor, an element with no usable name
    atp = argparse.ArgumentParser(add_help=False)
    atp.add_argument("--at", metavar="X,Y", help="a point inside the element: 30%%,70%% of its box, or 12,40 pixels "
                     "from its top left (maxpm browser screenshot <target> --grid shows the box)")
    x = brs.add_parser("click", parents=[bp, ap, ep, atp], help="click an element (its name, role and name as link "
                       "\"Comments\", or a ref such as @e3) or a point (x y, CSS pixels)")
    x.add_argument("target", nargs="?", help="\"Comments\" (a name), link (a role, then the name), @e3, or x")
    x.add_argument("y", nargs="?", help="the name after a role, or y")
    x.add_argument("--selector", help="a CSS selector; the first element that matches")
    # #5669: the other clicks, and the keys held
    x.add_argument("--double", action="store_true", help="a double click")
    x.add_argument("--right", action="store_true", help="a click of the right button (a context menu)")
    with_help = "keys held: shift, ctrl, alt, meta (comma between)"
    x.add_argument("--with", dest="mods", metavar="KEYS", help=with_help)
    wp = argparse.ArgumentParser(add_help=False)
    wp.add_argument("--with", dest="mods", metavar="KEYS", help=with_help)
    x = brs.add_parser("hover", parents=[bp, ap, ep, wp, atp], help="move the pointer onto an element and leave it there; "
                       "the reply lists what opened (a menu, a tooltip)")
    x.add_argument("target", nargs="?", help="the element (its name, role and name, @e3), or x")
    x.add_argument("y", nargs="?", help="the name after a role, or y")
    x = brs.add_parser("scroll", parents=[bp, ap, ep, atp], help="wheel input over the page (or over an element that "
                       "scrolls by itself): down (default), up, left, right, or pixels (-300 is up)")
    x.add_argument("words", nargs="*", metavar="[target] [amount]")
    x = brs.add_parser("drag", parents=[bp, ap, wp, atp], help="press on one element (or x,y), move with the button "
                       "held, release on another (or x,y): a slider, a card to another list")
    x.add_argument("source", metavar="from"); x.add_argument("dest", metavar="to")
    x.add_argument("--to-at", dest="to_at", metavar="X,Y", help="the point inside the element where the drag ends "
                   "(--at: where it starts)")
    x = brs.add_parser("type", parents=[bp, ap, ep], help="type text key by key: type \"Search\" 'text' clicks the "
                       "field first; type 'text' types where the focus is")
    x.add_argument("words", nargs="+", metavar="[target] text"); x.add_argument("--selector")
    x = brs.add_parser("fill", parents=[bp, ap, ep], help="set a field's text at once (its old text goes)")
    x.add_argument("target", help="\"Month\" (its name), @e5, or a CSS selector"); x.add_argument("text")
    x = brs.add_parser("select", parents=[bp, ap, ep], help="pick the options of a select, by value or label")
    x.add_argument("target"); x.add_argument("values", nargs="+")
    for name, h in (("check", "make a checkbox, a radio button or a switch checked"),
                    ("uncheck", "make a checkbox or a switch not checked")):
        brs.add_parser(name, parents=[bp, ap, ep], help=h).add_argument("target")
    x = brs.add_parser("key", parents=[bp, ap, wp], help="press a key: Enter, Tab, Escape, Backspace, ArrowDown, ..., "
                       "or one character; --with meta for a shortcut (meta a)")
    x.add_argument("key")
    x = brs.add_parser("upload", parents=[bp, ap, ep], help="give files to a file field (also a hidden one): each "
                       "file in the project's folder or the browser's downloads")
    x.add_argument("target", help="the file field: its name, role and name, @e5, or a CSS selector")
    x.add_argument("files", nargs="+")
    x = brs.add_parser("image", parents=[bp, ep], help="save an image element's file at full size (the largest of "
                       "its sizes) and print its path, type and size; open the file to see it")
    x.add_argument("target", help="the image, or a link or button with an image: its name, image \"name\", or @e5")
    x.add_argument("--out", help="the file to write, in the project's folder or the browser's downloads (default: "
                   "the downloads, under the image's own name)")
    x = brs.add_parser("dialog", parents=[bp, ap], help="answer the page's alert, confirm or prompt box (#5674): "
                       "accept [text], or dismiss")
    x.add_argument("answer", choices=["accept", "dismiss"])
    x.add_argument("text", nargs="?", help="the answer of a prompt box")
    x = brs.add_parser("eval", parents=[bp], help="run JavaScript in the page and print its value (JSON)")
    x.add_argument("js")
    x = brs.add_parser("batch", parents=[bp], help="several actions in one command: one on each line of the standard "
                       "input (or --file), as after 'maxpm browser'; the first that fails ends it")
    x.add_argument("--file", help="read the lines from this file, not the standard input")
    x.add_argument("--lines", help="the lines themselves, in one argument (the MCP tool)")
    # #5680: a session's walk as lines, and the lines as one call with inputs
    x = brs.add_parser("size", parents=[bp], help="the page's size: phone (390x844, touch), desktop, <width>x<height>, "
                       "or pane (the side panel's size again); it holds until the person uses the pane")
    x.add_argument("size")
    x.add_argument("--dpr", type=float, help="the pixel density (default 1; a screenshot is that many times larger)")
    x = brs.add_parser("errors", parents=[bp], help="the page's script errors, console.error calls and failed requests "
                       "(status 400 or more, or no answer), the newest last")
    x.add_argument("--clear", action="store_true", help="empty the list after it prints")
    x = brs.add_parser("record", parents=[bp], help="a step record: start, then each action adds its line, what it "
                       "changed, the page errors and a picture; stop puts the steps as a canvas; step <n> prints one "
                       "step and the path of its picture")
    x.add_argument("do", nargs="?", default="status", choices=("start", "stop", "status", "step"))
    x.add_argument("n", nargs="?", type=int, help="the step's number (record step <n>)")
    x.add_argument("--name", help="the record's name, also its canvas's name (default walk-<date>-<time>)")
    x = brs.add_parser("record-steps", parents=[bp], help="the person's own clicks, keys and addresses in this browser "
                       "as lines that run takes: start, the person works in the pane, stop prints the lines")
    x.add_argument("do", nargs="?", default="status", choices=("start", "stop", "status"))
    x = brs.add_parser("steps", parents=[bp], help="the actions of your walk in this browser, as lines that run takes "
                       "(names, not refs); put {name} where an input goes")
    x.add_argument("--clear", action="store_true", help="forget the walk so far (start a new one)")
    x = brs.add_parser("run", parents=[bp], help="the lines of a walk (stdin or --file) as one call: each {name} takes "
                       "the input name=value; the result is the last extract's rows; a failed step stops it")
    x.add_argument("inputs", nargs="*", metavar="name=value")
    x.add_argument("--file", help="read the lines from this file, not the standard input")
    x.add_argument("--lines", help="the lines themselves, in one argument (the MCP tool)")
    for name, h in (("back", "go back"), ("forward", "go forward"), ("reload", "load the page again")):
        brs.add_parser(name, parents=[bp, ap], help=h)
    for name, h in (("release", "give control up"), ("close", "close the browser (its profile and sign-ins stay)"),
                    ("window", "show it as a normal window (for a site that refuses a headless browser, a sign-in)"),
                    ("headless", "back into the side panel, with no window")):
        brs.add_parser(name, parents=[bp], help=h)
    x = brs.add_parser("control", parents=[bp], help="take control for your session: other sessions and the person's "
                       "input wait")
    x.add_argument("--take", action="store_true", help="also from the person, when they asked you in your chat or "
                   "have not used the browser for browser_take_after (2m)")
    x = brs.add_parser("give", parents=[bp], help="say that a subagent of your session drives it now (the panel shows "
                       "its label); your session keeps control")
    x.add_argument("label", help="the subagent, for example browser")
    x = brs.add_parser("tab", parents=[bp], help="show another tab (its id from maxpm browser status)"); x.add_argument("id")
    x = brs.add_parser("new-tab", parents=[bp], help="open a new tab"); x.add_argument("url", nargs="?", default="about:blank")
    x = brs.add_parser("close-tab", parents=[bp], help="close a tab (default: the one that shows)"); x.add_argument("id", nargs="?")
    x = brs.add_parser("annotations", parents=[bp], help="the pages that the person marked up in the Browser pane "
                       "and sent (#5655): yours and your item's; --all: every set of the project")
    x.add_argument("--all", action="store_true", help="every set of the project, not only yours")
    x.add_argument("--open", action="store_true", help="only sets with notes that are not fixed")
    x = brs.add_parser("annotation", help="one marked-up page: its picture, each note and its element, and its state; "
                       "mark a note: maxpm browser annotation <id> fix <n> --note \"<what you changed>\"")
    x.add_argument("id", help="the set, for example 7 or a7")
    x.add_argument("change", nargs="?", choices=["fix", "not-fixed", "reopen"],
                   help="fix: you fixed note <n>; not-fixed: you could not; reopen: it is open again")
    x.add_argument("n", nargs="?", type=int, help="the note's number (in the picture)")
    x.add_argument("--note", help="one line: what you changed, or why it is not fixed")
    x = brs.add_parser("fallbacks", help="the cases where the names did not work and a session fell back (html, an "
                       "element picture, a selector, a point): counts by site and kind, and the newest cases")
    x.add_argument("--site", help="one site: its host, or the end of it (wikipedia.org)")
    x.add_argument("--since", default="7d", help="a duration back from now (7d, 24h) or a time; default 7d")
    x.add_argument("-n", type=int, default=core.FALLBACKS_SHOW, help="how many of the newest cases to print")
    cv = sub.add_parser("canvas", help="canvases: pages that a session makes and maxpm keeps (HTML, Markdown, SVG), "
                        "with versions and the comments people write on them")
    cvs = cv.add_subparsers(dest="cvcmd", required=True)
    x = cvs.add_parser("put", help="store a new version: maxpm canvas put <file> --name <name> (- reads stdin)")
    x.add_argument("file", nargs="?", help="the page: a .html, .md or .svg file, or - for stdin (or use --text)")
    x.add_argument("--name", required=True, help="the canvas: the same name in the same project is the same canvas")
    x.add_argument("--text", help="the page's text itself, in place of a file")
    x.add_argument("--kind", help="html, md, or svg (default: from the file name)")
    x.add_argument("--title"); x.add_argument("--note", help="what changed in this version")
    x.add_argument("--item", type=int, help="the item it belongs to (default: the item you hold)")
    x.add_argument("--project", help="default: the item's project, or this folder's")
    x = cvs.add_parser("kit", help="the starter page of the canvas kit: a visual HTML page (task graph, cards, side by "
                       "side, bars, timeline, mockup, pictures with notes, decision cards) that you fill and put")
    x = cvs.add_parser("list", help="the canvases, newest change first")
    x.add_argument("--project"); x.add_argument("--item", type=int)
    x.add_argument("--mine", action="store_true", help="only the ones whose latest version you put")
    x.add_argument("--deleted", action="store_true", help="deleted ones too")
    x = cvs.add_parser("show", help="a canvas: its versions, its comments, and the text of a version")
    x.add_argument("name"); x.add_argument("--version", type=int); x.add_argument("--project")
    x.add_argument("--text", action="store_true", help="print only the page's text")
    x = cvs.add_parser("comments", help="the comments on a canvas with their replies (an agent's read marks sent ones read)")
    x.add_argument("name"); x.add_argument("--open", action="store_true", help="only the ones that wait for an answer")
    x.add_argument("--project")
    x = cvs.add_parser("comment", help="comment on a part of a canvas (--quote, --path) or on the whole page; it is a "
                       "draft until you send it")
    x.add_argument("name"); x.add_argument("text")
    x.add_argument("--quote", help="the words of the page that the comment is about")
    x.add_argument("--path", help="the part of the page (a CSS path, for example h2:nth-of-type(3))")
    x.add_argument("--version", type=int); x.add_argument("--project")
    x.add_argument("--send", action="store_true", help="send it at once")
    x = cvs.add_parser("send", help="send your draft comments (or the ones named) to the session that wrote the canvas")
    x.add_argument("name"); x.add_argument("comments", nargs="*", help="comment ids (c12); default: all your drafts")
    x.add_argument("--project")
    x = cvs.add_parser("reply", help="answer a comment in its thread"); x.add_argument("comment"); x.add_argument("text")
    x = cvs.add_parser("resolve", help="close a comment: say what changed, or why nothing needs to")
    x.add_argument("comment"); x.add_argument("--note")
    x = cvs.add_parser("open", help="put a canvas in front of the person: the page opens it in the side panel of your "
                       "session (or --session); maxpm serve must run")
    x.add_argument("name"); x.add_argument("--project")
    x.add_argument("--session", help="the agent whose side panel shows it (default: you)")
    x = cvs.add_parser("section", help="a doc (#4708): replace one section of a Markdown canvas, or add it; --pending "
                       "shows it as being written")
    x.add_argument("name"); x.add_argument("heading", help="the section's heading, for example 'Decisions'")
    x.add_argument("file", nargs="?", help="the section's text: a file, or - for stdin (or use --text)")
    x.add_argument("--text"); x.add_argument("--pending", help="what the section will say; the page shows it as being written")
    x.add_argument("--note"); x.add_argument("--project")
    x = cvs.add_parser("answer", help="answer a decision of a doc (Decision <n> of <m>: ...) in place; it goes to the "
                       "session that writes the doc")
    x.add_argument("name"); x.add_argument("decision", type=int); x.add_argument("text"); x.add_argument("--project")
    x = cvs.add_parser("delete", help="take a canvas off every list"); x.add_argument("name"); x.add_argument("--project")
    x = cvs.add_parser("save", help="write a version of a canvas as a file (for a page that goes into git)")
    x.add_argument("name"); x.add_argument("path"); x.add_argument("--version", type=int); x.add_argument("--project")
    x.add_argument("--force", action="store_true", help="write over the file")
    x = cvs.add_parser("publish", help="show a canvas off this computer, as the button Share artifact does: publish a "
                       "version as a private realquicksite page and print its link (the person shares it there)")
    x.add_argument("name"); x.add_argument("--version", type=int, help="default: the newest")
    x.add_argument("--project")
    x.add_argument("--yes", action="store_true", help="a person's yes to a new version of a page that is shared")
    rq = sub.add_parser("realquicksite", help="the connection that publishes canvases on realquicksite")
    rqs = rq.add_subparsers(dest="rqcmd", required=True)
    rqs.add_parser("status", help="connected or not, and the account (never the key)")
    rqs.add_parser("connect", help="print the address that connects MaximizePM to realquicksite (maxpm serve must run)")
    rqs.add_parser("disconnect", help="delete the key file (then click Remove on the realquicksite setup screen)")

    se = sub.add_parser("secret", help="secrets saved by name (a token, a client secret, a key file) and passed to a "
                        "command or a .env file; no command prints a value (#5757)")
    ses = se.add_subparsers(dest="secmd", required=True)
    x = ses.add_parser("put", help="save a value under a name: it asks for it (no echo), reads stdin, or reads the "
                       "project's browser; a new value replaces the old one")
    x.add_argument("name", help="letters, digits, '.', '_', '-': for example cloudflare-api-token")
    x.add_argument("--from-browser", metavar="TARGET", help="read it from the project's browser: a field or text by its "
                   "name as maxpm browser snapshot prints it, a ref (@e3), css:<selector>, or page (the whole text)")
    x.add_argument("--match", help="with --from-browser: a regex over that text; its first group (else the match)")
    x.add_argument("--in", dest="within", help="with --from-browser: the text of the row or card around the element")
    x.add_argument("--project", help="the project it belongs to (default: this folder's)")
    x.add_argument("--note", help="what it is and where it came from (never the value)")
    x = ses.add_parser("pipe", help="run a command with the value on its stdin: maxpm secret pipe <name> -- wrangler "
                       "secret put GOOGLE_CLIENT_SECRET")
    x.add_argument("name")
    x.add_argument("command", nargs=argparse.REMAINDER, help="the command, after --")
    x = ses.add_parser("env", help="write NAME=value into a .env file (replaces that key's line, or adds it)")
    x.add_argument("name")
    x.add_argument("file", help="the .env file")
    x.add_argument("--key", help="the variable (default: the name in capitals, '-' and '.' as '_')")
    x = ses.add_parser("list", help="the names, their project, who saved and used each, when (never the values)")
    x.add_argument("--project")
    x = ses.add_parser("log", help="each put, use and delete, newest first")
    x.add_argument("name", nargs="?")
    x.add_argument("-n", type=int, default=30)
    x = ses.add_parser("delete", help="delete the value and its name")
    x.add_argument("name")

    gl = sub.add_parser("goal", help="goals: outcomes in a project that one agent owns and works toward")
    gls = gl.add_subparsers(dest="gcmd", required=True)
    x = gls.add_parser("add", help="maxpm goal add <project> <name> --outcome \"...\" --done-when \"...\"")
    x.add_argument("project"); x.add_argument("name"); x.add_argument("--outcome", default="")
    x.add_argument("--done-when", dest="done_when", default="", help="the test that shows the outcome is reached")
    x.add_argument("--rank", type=int, help="position among the project's goals (1 = first)")
    x.add_argument("--shared", action="store_true", help="no owner, ever: maxpm go gives the goal to no agent, and "
                   "its items stay open to every agent")
    x.add_argument("--parent", help="a sub-goal of this goal (one level, same project): its sessions read the "
                   "parent's handoff, then its own")
    x = gls.add_parser("list", help="open goals in order (--all: complete ones too)")
    x.add_argument("--project"); x.add_argument("--all", action="store_true")
    x = gls.add_parser("show", help="a goal, its owner, and its items"); x.add_argument("name")
    x = gls.add_parser("rank", help="move a goal among its project's goals (1 = first)"); x.add_argument("name"); x.add_argument("rank", type=int)
    x = gls.add_parser("edit"); x.add_argument("name"); x.add_argument("--outcome"); x.add_argument("--done-when", dest="done_when")
    x.add_argument("--rename")
    x.add_argument("--shared", dest="shared", action="store_true", default=None,
                   help="no owner, ever: maxpm go gives the goal to no agent, nobody can own it, and its items stay "
                   "open to every agent (several agents work on it at the same time). A person or a manager sets it")
    x.add_argument("--owned", dest="shared", action="store_false", help="undo --shared: one agent can own the goal again")
    for verb in ("own", "take"):
        x = gls.add_parser(verb, help="own a goal: you create and take the items that reach it; its agent items "
                           "are reserved for you"); x.add_argument("name")
        x.add_argument("--lease", help="how long the claim lasts without a maxpm command, and your leases on its "
                       "items (default: the goal_lease setting, 4h)")
    x = gls.add_parser("give", help="hand a goal you own to another agent"); x.add_argument("name")
    x.add_argument("--to", required=True)
    x.add_argument("--no-handoff", dest="no_handoff", metavar="WHY",
                   help="give it although the handoff is older than your last finished item of the goal")
    x = gls.add_parser("release", help="stop owning a goal"); x.add_argument("name")
    x.add_argument("--no-handoff", dest="no_handoff", metavar="WHY",
                   help="release it although the handoff is older than your last finished item of the goal")
    x = gls.add_parser("base", help="a goal's base: the context session its workers start from as forks")
    x.add_argument("name")
    x.add_argument("--ready", action="store_true", help="in the context session, at its end: record this session as the base")
    x.add_argument("--clear", action="store_true", help="drop the base: the next workers start fresh")
    x = gls.add_parser("handoff", help="read a goal's handoff, or store a new version: what the goal is, the decisions "
                       "so far, the files that matter, and what is left"); x.add_argument("name")
    x.add_argument("text", nargs="?", help="the new handoff (or --file)")
    x.add_argument("--file", help="read the new handoff from this file")
    x.add_argument("--version", type=int, help="show this version")
    x.add_argument("--versions", action="store_true", help="list the versions")
    x = gls.add_parser("done", help="declare a goal complete (refused while its items are open)"); x.add_argument("name")
    x.add_argument("--result", required=True, help="one line: what the goal achieved")
    x.add_argument("--drop-open", action="store_true", help="drop the goal's open items, with the result as the note")
    x = gls.add_parser("reopen"); x.add_argument("name")

    sc = sub.add_parser("schedule", help="scheduled tasks: a repeating obligation (an annual report, taxes, a "
                        "renewal, an access review) adds its item at each due time, less its lead time")
    scs = sc.add_subparsers(dest="schcmd", required=True)
    x = scs.add_parser("add", help="add a schedule: maxpm schedule add <project> \"<title>\" --yearly 03-15 --lead 30d")
    x.add_argument("project"); x.add_argument("title", help="the title of each item it adds")
    x.add_argument("--cron", help="the due times, five fields in the project's time zone: '0 9 1 * *' (09:00 on the "
                   "first of each month)")
    x.add_argument("--yearly", help="a yearly due date, MM-DD: due by the end of that day")
    x.add_argument("--lead", default="0s", help="the item comes this long before its due time, for example 30d "
                   "(default 0s: at the due time)")
    x.add_argument("--doer", choices=core.DOERS, default="any", help="who does each item (default any)")
    x.add_argument("-p", "--priority", type=int, default=2, choices=range(5), help="0 highest, 4 lowest (default 2)")
    x.add_argument("--context", default="", help="why, where, and what to do: it goes into each item")
    x = scs.add_parser("list", help="the schedules, and when each one adds its next item")
    x.add_argument("--project")
    x = scs.add_parser("rm", help="remove a schedule; the items it added stay")
    x.add_argument("id", help="the schedule, s<n> or <n>")
    op = sub.add_parser("ops", help="operations checks of a project or a deploy target: what MaximizePM watches "
                        "(a url, a beat, a command, a log, or a Monitoring Plugins executable), when it is a fault, "
                        "and who is told. Any session adds a url or beat check; a cmd, log, or plugin check and a "
                        "repair need a person or the manager; a repair on a deploy target needs a person. After each release "
                        "of a target, its checks and its projects' checks run at once (the release watch)")
    ops = op.add_subparsers(dest="ocmd", required=True)

    def ops_fields(x, add):
        x.add_argument("--url", help="url: the address of the HTTP GET")
        x.add_argument("--statuses", help=f"url: the healthy HTTP statuses (default {core.OPS_DEFAULTS['statuses']}; "
                       "for example 200,204)")
        x.add_argument("--body", help="url: a text that the healthy answer holds")
        x.add_argument("--command", help="cmd: a shell command, healthy on exit 0; log: a command that prints the "
                       "new log lines; plugin: the executable and its arguments")
        x.add_argument("--pattern", help="log: a regular expression; the lines that match count")
        x.add_argument("--limit", type=int, help="log: the count of lines in the window that is a failure (default 1)")
        x.add_argument("--window", help="log: the time the count covers (default 15m)")
        x.add_argument("--normal", type=float, help="log, with --errors ours: the usual count of matching lines in "
                       "one hour; the check fails when its window holds more than that rate")
        x.add_argument("--errors", choices=tuple(core.OPS_ERRORS), help="the kind of error, when failures are a "
                       "fault: runs (fail-after failed runs in a row, the default), never (the first failed run), "
                       "ours (log: more lines than the normal rate), outside (log: limit different sources, the first "
                       "group of the pattern), release (one failed run after a release)")
        x.add_argument("--period", help="beat: the longest time between two beats, for example 1h")
        d = core.OPS_DEFAULTS
        x.add_argument("--every", help="how often it runs" + (f" (default {d['every']})" if add else ""))
        x.add_argument("--cron", help="when it runs instead, as a cron expression in the project's time zone: "
                       "'0 3 * * *' (03:00 each day), '*/15 9-17 * * mon-fri'; empty: every again. After a stop it "
                       "runs once, and the run records how many times were due")
        x.add_argument("--fail-after", dest="fail_after", type=int,
                       help="failed runs in a row before it is a fault" + (f" (default {d['fail_after']})" if add else ""))
        x.add_argument("--recover-after", dest="recover_after", type=int, help="passed runs in a row before a fault "
                       "counts as healed" + (f" (default {d['recover_after']})" if add else ""))
        x.add_argument("--timeout", help="the time limit of one run" + (f" (default {d['timeout']})" if add else ""))
        x.add_argument("--severity", choices=core.OPS_SEVERITIES, help="page: it may reach the phone; item: an item "
                       "and the manager; note: a line on the page" + (f" (default {d['severity']})" if add else ""))
        x.add_argument("--runbook", help="for the agent that investigates a fault: where to look, what is normal")
        x.add_argument("--subject", choices=("on", "off"), help="cmd, log, plugin: each line of the output names a "
                       "subject, and each subject has its own fault")
        x.add_argument("--repair", help="a command that fixes a known fault (a person or the manager sets it; on a "
                       "deploy target, a person); empty removes it")
        x.add_argument("--layer", choices=list(core.OPS_LAYERS), help="what it watches: app (the project itself, the "
                       "default); dependency (an outside vendor), capacity (a disk, memory, a quota) or service (a "
                       "process, the database service): while it fails, the faults of the other checks of the project "
                       "get its kind")
        x.add_argument("--outside", choices=("on", "off"), help="on: the watchdog runs it (maxpm ops check --outside, "
                       "a launchd job: maxpm serve --install-watchdog), not maxpm serve, so it also runs while serve "
                       "is down; for a check of serve itself or of the relay")

    x = ops.add_parser("add", help="add a check: maxpm ops add <project|target> <name> <form> --url ... "
                       "(forms: " + "; ".join(f"{k}: {v}" for k, v in core.OPS_FORMS.items()) + ")")
    x.add_argument("scope", help="a project or a deploy target (project:<name> or target:<name> when both exist)")
    x.add_argument("name"); x.add_argument("form", choices=list(core.OPS_FORMS))
    ops_fields(x, True)
    x = ops.add_parser("propose", help="propose a check, or a change of one: it never runs; a person or the manager "
                       "accepts it (maxpm ops accept) or rejects it. Any form: the way for an agent to ask for a cmd, "
                       "log, or plugin check or a repair")
    x.add_argument("scope"); x.add_argument("name")
    x.add_argument("form", nargs="?", choices=list(core.OPS_FORMS), help="the form of a new check; leave it out to "
                   "change a check that exists")
    x.add_argument("--why", required=True, help="what the check would catch, or what it watches")
    ops_fields(x, True)
    x = ops.add_parser("accept", help="a person or the manager: the proposed check becomes the check")
    x.add_argument("scope"); x.add_argument("name"); x.add_argument("--note", help="a word to the proposer")
    x = ops.add_parser("reject", help="a person or the manager: the proposed check is not wanted")
    x.add_argument("scope"); x.add_argument("name"); x.add_argument("--note", required=True, help="why")
    x = ops.add_parser("setup", help="a guided start: which project, what to watch (plain questions for the four "
                       "forms), one run of each new check before it is kept, a test notice to your phone, and the "
                       "outside switch. In a terminal it asks one question at a time; an agent gives the answers "
                       "as options (--url, --period, --command and --pattern, --test-notice, --deadman-url)")
    x.add_argument("scope", nargs="?", help="a project or a deploy target")
    x.add_argument("--name", help="the check's name (default: from its address or command; a beat check needs one)")
    x.add_argument("--form", choices=list(core.OPS_FORMS), help="the form (default: from the options: --url url, "
                   "--period beat, --pattern log, --command cmd)")
    ops_fields(x, True)
    x.add_argument("--anyway", action="store_true", help="keep the check also when its first run fails")
    x.add_argument("--why-propose", dest="why_propose", help="when you may not add the check (a command, a repair): "
                   "propose it with this reason instead")
    x.add_argument("--test-notice", dest="test_notice", action="store_true", help="send one test notice to the phone")
    x.add_argument("--deadman-url", dest="deadman_url", help="the ping address of a heartbeat service outside this "
                   "computer (ops_deadman_url); setup sends one beat to it at once")
    x.add_argument("--profile", help="apply a profile: a file, or the name of one that ships with MaximizePM "
                   "(maximizepm: MaximizePM watches itself, in the project of that name, which setup adds)")
    x.add_argument("--yes", action="store_true", help="ask nothing: do only what the options say")
    x = ops.add_parser("edit", help="change the settings of a check")
    x.add_argument("scope"); x.add_argument("name")
    x.add_argument("--form", choices=list(core.OPS_FORMS), help="another form; give its settings too")
    ops_fields(x, False)
    x = ops.add_parser("record", help="the record of every detection: each check or subject a run found not ok "
                       "(also noise, planned and warnings), its kind, first and last seen, how often, and its item")
    x.add_argument("--project", help="the project and its deploy target (default: all)")
    x.add_argument("--kind", help="one kind (maxpm ops kinds)")
    x.add_argument("--since", help="last seen within this time, for example 7d")
    x.add_argument("--check", help="one check's name")
    x.add_argument("-n", type=int, default=50, help="at most this many (default 50)")
    x = ops.add_parser("list", help="the checks, of one project or deploy target or of all")
    x.add_argument("scope", nargs="?")
    x = ops.add_parser("show", help="one check, its last runs and its last changes of state")
    x.add_argument("scope"); x.add_argument("name")
    x = ops.add_parser("rm", help="remove a check (its runs and changes of state stay)")
    x.add_argument("scope"); x.add_argument("name")
    x = ops.add_parser("probe", help="one of MaximizePM's own checks, on the Monitoring Plugins API (a status line, "
                       "a line for each subject, exit 0 OK or 2 CRITICAL), for a cmd check with --subject on: "
                       + "; ".join(f"{k}: {v}" for k, v in core.OPS_PROBES.items()))
    x.add_argument("name", choices=list(core.OPS_PROBES))
    x = ops.add_parser("logs", help="a plugin: the new lines of a log since its last run against a pattern and limits "
                       "(maxpm ops add <scope> <name> plugin --command \"maxpm ops logs --file <path>\")")
    x.add_argument("--file", help="a log file; the next run reads from where this one stopped (a rotated file from "
                   "its start)")
    x.add_argument("--ssh", help="the host of --file: read it through ssh in batch mode")
    x.add_argument("--command", help="a command that prints the log, such as docker logs app --since {since} or "
                   "journalctl -u app --since @{since_epoch}; the lines after the last run's last lines count")
    x.add_argument("--pattern", help="the lines that count, a regular expression (default: ERROR, CRITICAL, FATAL, "
                   "Traceback, panic)")
    x.add_argument("--ignore", help="the lines that never count, a regular expression")
    x.add_argument("--warning", type=float, help="WARNING from this many matching lines")
    x.add_argument("--critical", type=float, help="CRITICAL from this many matching lines (with neither: 1)")
    x.add_argument("--per-minute", action="store_true", help="the limits are lines a minute since the last run")
    x.add_argument("--from-start", action="store_true", help="the first run reads the old lines too")
    x.add_argument("--name", help="the name of its read position (default: the check that runs it)")
    x.add_argument("--timeout", type=float, default=60, help="seconds for the command or ssh (default 60)")
    x = ops.add_parser("pages", help="a page walk: each page of a list opens in a background tab of the project's "
                       "browser, and its browser errors and failed requests, with their stack frames mapped to the "
                       "source, are one subject each, as a plugin check (maxpm ops add <project> <name> plugin "
                       "--command \"maxpm ops pages run <name> --project <project> --pages <address>,...\" --subject on "
                       "--timeout 15m: the first run after a release builds the source maps)")
    pgs = x.add_subparsers(dest="pgcmd", required=True)
    v = pgs.add_parser("run", help="the plugin: walk the pages and print one subject for each error group (exit 0 "
                       "no error, 2 an error group, 3 the walk did not run)")
    v.add_argument("name", help="the check's name")
    v.add_argument("--project", required=True, help="the project whose browser walks, and whose release gives the "
                   "source maps")
    v.add_argument("--pages", required=True, help="the addresses, separated by commas")
    v.add_argument("--settle", type=float, default=3.0, help="seconds after each load for errors that come late "
                   "(default 3)")
    v.add_argument("--timeout", type=float, default=60.0, help="seconds for the whole walk (default 60)")
    v.add_argument("--rev", help="map with the build of this commit, not the one in production")
    v = pgs.add_parser("maps", help="build the source maps of the commit in production now, with the command of the "
                       "system description (code.source_maps 'command: ...'); a run builds them otherwise")
    v.add_argument("project")
    v.add_argument("--rev", help="this commit, not the one in production")
    x = ops.add_parser("visual", help="a page looks as it was approved: screenshots at set widths and themes against "
                       "approved ones, as a plugin check (maxpm ops add <scope> <name> plugin --command \"maxpm ops "
                       "visual run <name> --url <address>\" --timeout 2m)")
    vis = x.add_subparsers(dest="vcmd", required=True)
    v = vis.add_parser("run", help="the plugin: shoot the page and compare each shot with its approved picture "
                       "(exit 0 OK, 1 no approved picture yet, 2 a shot differs, 3 the page failed)")
    v.add_argument("name", help="the visual check's name: its pictures are in <queue folder>/visual/<name>/")
    v.add_argument("--url", required=True, help="the page's address")
    v.add_argument("--widths", default="1440,390", help="CSS pixels, a comma list (default 1440,390)")
    v.add_argument("--themes", default="light,dark", help="light, dark, or light,dark (default)")
    v.add_argument("--threshold", type=float, default=0.01,
                   help="a shot differs when more than this percent of its pixels differ (default 0.01: one changed "
                        "word)")
    v.add_argument("--wait", type=float, default=1.0, help="seconds after the load before the screenshot (default 1)")
    v.add_argument("--full", action="store_true", help="the whole page, not only the window")
    v.add_argument("--mask", action="append", default=[],
                   help="a CSS selector of a part that changes on each load (a clock): hidden before the screenshot")
    v.add_argument("--timeout", type=float, default=30, help="seconds for each load (default 30)")
    v = vis.add_parser("show", help="the last run: for each shot its difference and its three pictures (approved, "
                       "new, and the new one with the pixels that differ in red)")
    v.add_argument("name")
    v = vis.add_parser("approve", help="a person or the manager: the new pictures become the approved ones")
    v.add_argument("name")
    v.add_argument("--shot", action="append", help="only this shot, <width>-<theme> (repeatable; default: all)")
    v.add_argument("--note", help="why the page changed on purpose")
    x = ops.add_parser("pause", help="stop a check for planned work, until a time; --resume starts it again")
    x.add_argument("scope"); x.add_argument("name")
    x.add_argument("--until", help="a duration (2h) or a time (tomorrow 7:00)")
    x.add_argument("--resume", action="store_true", help="start the check again now")
    x = ops.add_parser("deps", help="the dependencies in a project's system description (infra.dependencies, one a "
                       "line: '<vendor>: <status page> <addresses>'), each with its check; --add: a url check of each "
                       "status page that has none, of layer dependency")
    x.add_argument("project"); x.add_argument("--add", action="store_true", help="add the missing checks")
    x = ops.add_parser("cause", help="state the cause of a fault in the fixed form, with a proof that MaximizePM runs "
                       "now; fix items of the fault wait until a proof passes, and the item opens again when the check "
                       "fails after the action")
    x.add_argument("id", type=int, help="the fault item")
    x.add_argument("--cause", required=True, help="what went wrong")
    x.add_argument("--proof", required=True, help="a check (its key or <scope>/<check>: maxpm serve runs it) or a "
                   "command (it runs here, in your sandbox, within ops_proof_timeout)")
    x.add_argument("--expect", help="what the proof gives: exit N, a status of a check (ok, warning, critical, "
                   "unknown), or a text of its output (default: a check is critical, a command exits 0)")
    x.add_argument("--action", required=True, help="what fixes it")
    x.add_argument("--check", help="the check that must pass after the action (default: the fault's own check)")
    x = ops.add_parser("kinds", help="the kinds of problems, and the eight rules that give each fault its kind")
    x = ops.add_parser("kind", help="give a fault item its kind (the sorting session of an unknown fault, or a person "
                       "or the manager who corrects one); the session that holds it releases it")
    x.add_argument("id", type=int, help="the fault item"); x.add_argument("kind", help="the kind (maxpm ops kinds)")
    x.add_argument("--why", required=True, help="what shows the kind")
    x.add_argument("--message", help="the text of the output that shows it: it becomes a known message of the "
                   "project, so the next fault of this kind needs no session")
    x = ops.add_parser("repair", help="ask maxpm serve to run the repair command set on a check: the session that "
                       "holds its fault item, at most ops_repair_max times in ops_repair_window; a person at any time")
    x.add_argument("scope", help="a project or a deploy target, or the check's name when it is the only one of "
                   "that name"); x.add_argument("name", nargs="?")
    x.add_argument("--reason", required=True, help="what you found: why the repair helps")
    x = ops.add_parser("check", help="the watchdog: run the checks marked outside that are due, with no maxpm serve; "
                       "repair one in fault (at most ops_outside_repair_max in ops_outside_repair_window), then send "
                       "a notice on notify_channels. A launchd job runs it every minute (maxpm serve --install-watchdog)")
    x.add_argument("--outside", action="store_true", required=True, help="the checks marked outside (the only ones "
                   "it runs: maxpm serve runs the others)")
    x = ops.add_parser("run", help="ask maxpm serve to run a check now, also when it is not due or is paused")
    x.add_argument("scope", help="a project or a deploy target, or the check's name when it is the only one of "
                   "that name"); x.add_argument("name", nargs="?")
    x = ops.add_parser("beat", help="the project's own job says it ran: a beat check is healthy while beats come "
                       "within its period (the same op on /api/action: ops_beat)")
    x.add_argument("scope", help="a project or a deploy target, or the check's name when it is the only one of "
                   "that name"); x.add_argument("name", nargs="?")
    x = ops.add_parser("history", help="the last runs of a check and its changes of state, also of its subjects")
    x.add_argument("scope"); x.add_argument("name")
    x.add_argument("-n", type=int, default=20, help="how many runs and changes (default 20)")
    x = ops.add_parser("noise", help="noise rules: a check, a subject of it, or what matches a pattern is not a "
                       "problem until the rule ends; a matching fault opens no item and starts no agent, and a log "
                       "check does not count the matching lines")
    nz = x.add_subparsers(dest="oncmd", required=True)
    y = nz.add_parser("add", help="add a rule (an agent's rule ends within ops_noise_max, 30d; the manager gets a note)")
    y.add_argument("scope"); y.add_argument("name", help="the check")
    y.add_argument("--reason", required=True, help="what it is, and why nobody needs to act")
    y.add_argument("--pattern", help="a regular expression over the log lines and the run's summary; for a check with "
                   "subjects, over each subject's name (#5551), its key (ops:<scope>:<check>:#5551, as a fault item's "
                   "title starts) and its output line (#5551: <text>)")
    y.add_argument("--subject", help="only this subject of the check")
    y.add_argument("--until", help="when the rule ends: 30d, or a date (default ops_noise_max)")
    y.add_argument("--fault", type=int, help="the fault item that showed it (default: the fault item you hold)")
    y = nz.add_parser("list", help="the rules in force, of one project or target or of all")
    y.add_argument("scope", nargs="?"); y.add_argument("--all", action="store_true", help="also ended and removed ones")
    y = nz.add_parser("rm", help="end a rule now (its writer, a person or the manager)")
    y.add_argument("rule", help="n<id>"); y.add_argument("--note", required=True)
    x = ops.add_parser("system", help="the system description of a project: the facts of its code, its "
                       "infrastructure and its own rules that the kinds of problems and the evidence pack read. Kept "
                       "here, never in a repository; an agent's change is proposed until a person or the manager "
                       "accepts it")
    sys_ = x.add_subparsers(dest="oscmd", required=True)
    y = sys_.add_parser("show", help="the description in force, its versions and an open proposal")
    y.add_argument("project")
    y.add_argument("--version", type=int, help="that version (1 is the first one written)")
    y.add_argument("--proposed", action="store_true", help="show the open proposal")
    y = sys_.add_parser("edit", help="a new version: the one in force with your changes")
    y.add_argument("project")
    y.add_argument("--set", action="append", default=[], metavar="PART.FIELD=TEXT", dest="sets",
                   help="one field, for example code.test=\"make test\"; an empty text removes it. The fields: "
                   + ", ".join(f"{p}.{f}" for p in core.OPS_SYSTEM_FIELDS for f in core.OPS_SYSTEM_FIELDS[p]))
    y.add_argument("--file", help="the whole description as JSON ({\"code\": {...}, \"infra\": {...}, "
                   "\"rules\": {...}}), or - for stdin")
    y.add_argument("--why", help="why it changes (an agent must say: the fact that was missing, and the fault "
                   "that showed it)")
    y = sys_.add_parser("accept", help="a person or the manager: the proposed version is in force")
    y.add_argument("project"); y.add_argument("--note")
    y = sys_.add_parser("reject", help="a person or the manager: the proposal ends; nothing changes")
    y.add_argument("project"); y.add_argument("--note", required=True)
    x = ops.add_parser("profile", help="every check of a project or a deploy target, and its ops_* settings, as one "
                       "JSON file: export it, keep it in the project's repository, apply it on another machine")
    pps = x.add_subparsers(dest="opcmd", required=True)
    y = pps.add_parser("export", help="print the profile of a project or a deploy target")
    y.add_argument("scope"); y.add_argument("--out", help="write it to this file instead")
    y = pps.add_parser("apply", help="make the checks of the scope what the file says: it shows the checks it adds, "
                       "changes and removes, and writes them only with --yes. Each check keeps the rights of "
                       "maxpm ops add")
    y.add_argument("file", help="the profile file, - to read it from stdin, or the name of a profile that ships "
                   "with MaximizePM: " + ", ".join(core.ops_builtin_profiles()))
    y.add_argument("--scope", help="apply it to this project or target instead of the one the file names")
    y.add_argument("--yes", action="store_true", help="write the change")

    tg = sub.add_parser("target", help="deploy targets: where projects ship to")
    tgs = tg.add_subparsers(dest="tcmd", required=True)
    x = tgs.add_parser("add"); x.add_argument("name")
    x.add_argument("--description", default="", help="how and where it deploys")
    x = tgs.add_parser("describe", help="set how a target deploys"); x.add_argument("name"); x.add_argument("text")
    x = tgs.add_parser("deployer", help="what maxpm serve starts for the target's deploys: launch (the default) "
                       "alerts the owner when a deploy is ready, and starts a deployer session when there is no owner "
                       "or the owner cannot take it (gone, or idle at its prompt); standing starts that session as "
                       "soon as a release has a deploy item, so it waits during the review and deploys the moment the "
                       "review passes; off starts nothing")
    x.add_argument("name"); x.add_argument("mode", nargs="?", choices=core.DEPLOYER_MODES, help="none shows it")
    x = tgs.add_parser("watch", help="the times after each release that the checks of the target and its projects "
                       "run as the watch of that release, for example 1m 10m 1h 24h: a newer release ends the older "
                       "series, a time that passed with no run (serve did not run) runs once, marked late; off: at "
                       "once, then every ops_watch_every")
    x.add_argument("name"); x.add_argument("times", nargs="*", help="durations after the release; off; none shows them")
    x = tgs.add_parser("cadence", help="the shortest time between two normal releases of the target (2h: at most 12 "
                       "a day; 6h, 1d, 1w, 1mo; off: each release starts at once): ship requests collect on the open "
                       "deploy item, and its review (the deploy item when there is no review) is ready at the end of "
                       "the last release plus the cadence")
    x.add_argument("name"); x.add_argument("cadence", nargs="?", help="a number and m, h, d, w, or mo; off; none shows it")
    x.add_argument("--reason", help="why this cadence fits the project's needs (a service with users: short, so a fix "
                   "is live soon; a public tool repository: off, each push), shown in maxpm target show")
    x = tgs.add_parser("cut", help="fix the list of items of the collected release now (the target owner, the "
                       "manager, or a person): later ship requests join the next deploy item, which waits until this "
                       "release is done. The cut also comes by itself when a reviewer takes the review of the release")
    x.add_argument("name")
    x.add_argument("--rev", help="the commit that the release builds from, or any word for it: it goes into the history "
                   "and the notes of the deploy item")
    x = tgs.add_parser("hook", help="a shell command that MaximizePM runs when an event of the target occurs: "
                       + ", ".join(core.HOOK_EVENTS) + ". The maxpm command that causes the event runs it before it "
                       "returns (maxpm serve runs it when that command is in the sandbox of an agent session), in "
                       "the folder of the target's project, with MAXPM_EVENT, MAXPM_TARGET, MAXPM_REV, MAXPM_ITEM, "
                       "and MAXPM_AGENT in its environment, for at most hook_timeout. A hook never undoes and never "
                       "refuses its event; a failure alerts the target owner. A hook that exits 75 (busy) runs again "
                       "about every 2 minutes until hook_timeout. Only a person or the manager sets, "
                       "changes, or removes a hook")
    x.add_argument("name")
    x.add_argument("event", nargs="?", help="one of " + ", ".join(core.HOOK_EVENTS) + "; none lists the hooks")
    x.add_argument("command", nargs="?", help="the command; none shows the hook")
    x.add_argument("--clear", action="store_true", help="remove the hook of this event")
    x = tgs.add_parser("release-now", help="start the collected release sooner than the cadence permits, with a "
                       "reason (a fix of a production defect, a person's request); the target owner, the manager, "
                       "or a person decides it, and the history records it")
    x.add_argument("name"); x.add_argument("--reason", required=True, help="why this release cannot wait")
    x = tgs.add_parser("show", help="a target, its owner, its projects, and when its next release can start")
    x.add_argument("name")
    x = tgs.add_parser("rename", help="give a target a new name; its projects, deploy items and deploy project follow")
    x.add_argument("name"); x.add_argument("new")
    x = tgs.add_parser("own", help="become the one owner of a target (runs its deploys)"); x.add_argument("name")
    x.add_argument("--takeover", metavar="WHY", help="take the target from an owner who is away or gone; "
                   "says why, and the old owner is told")
    x = tgs.add_parser("release", help="stop owning a target"); x.add_argument("name")
    x = tgs.add_parser("give", help="hand a target you own to another agent (a person can give any target)"); x.add_argument("name")
    x.add_argument("--to", required=True)
    tgs.add_parser("list")

    x = sub.add_parser("add", help="add an item: maxpm add [project] \"title\" (project from --blocks/--found-during or the folder)")
    x.add_argument("words", nargs="*", metavar="[project] title")
    x.add_argument("--short", help="the short title every page shows: about 6 plain words, no ids, no file names, no "
                   "quotes (the title stays the full text, on the item page and in a hover)")
    x.add_argument("--no-goal-context", dest="no_goal_context", action="store_true",
                   help="an item of a goal that needs no goal context: its session starts fresh, not as a fork of the base")
    x.add_argument("--from", dest="plan_file", metavar="FILE",
                   help="add every line of a plan file (an outline) as an item; a line waits on the lines indented under it")
    x.add_argument("--dry-run", action="store_true", help="with --from: show what would be added (refused without --from)")
    x.add_argument("--goal", action="append", help="goal this item works toward (repeatable; default: the goal you own in the item's project)")
    x.add_argument("--no-goal", action="store_true", help="no goal tag, even when you own a goal (a fix found in passing)")
    x.add_argument("--priority", "-p", type=int, default=2, help="0 highest .. 4 lowest (default 2)")
    x.add_argument("--after", type=int, nargs="*", default=[], help="items this one waits on")
    x.add_argument("--feeds", type=int, nargs="*", default=[], help="items this one waits on and whose output it reads")
    x.add_argument("--due", help="due date: 2026-10-15 (end of day), 'fri 17:00 America/New_York'; warns, does not reorder")
    x.add_argument("--notes", default="")
    x.add_argument("--doer", default="any", choices=core.DOERS, help="who can do it (default any)")
    x.add_argument("--context", default="", help="what a new agent must know to start: why, where, decisions made")
    x.add_argument("--touches", nargs="*", default=[], help="files or directories it changes")
    x.add_argument("--check", default="", help="command that shows it works (tests, a build); for a fix, the "
                   "operations check that proves it, ops:<scope>:<check>: it runs after the release that ships it")
    _model_args(x)
    x.add_argument("--blocks", type=int, help="item that must wait on this new one (usually the one you hold)")
    x.add_argument("--found-during", type=int, dest="found_during",
                   help="the item you were working on when you found this (links them; not a dependency)")
    _ref_args(x)
    g = x.add_mutually_exclusive_group()
    g.add_argument("--keep", dest="mode", action="store_const", const="keep",
                   help="with --blocks: keep holding that item and do this one yourself now")
    g.add_argument("--release", dest="mode", action="store_const", const="release",
                   help="with --blocks: give that item back; anyone can do this one (the default)")
    x.add_argument("--backlog", nargs="?", const="", metavar="WHY",
                   help="add it to the backlog: kept, but nobody is offered it until maxpm activate <id> (put the "
                        "title first: maxpm add \"title\" --backlog \"why not now\")")
    x.add_argument("--new", metavar="HOW", help="add it although maxpm add found open or lately done items like it: "
                   "say how it differs (the history keeps it)")
    x.add_argument("--tier", choices=core.TIERS, help="expert: a task that a person talks through with an expert "
                   "session (a session started for it takes it; a worker never does); test: the target session of a "
                   "live test, which waits at its prompt by design (a session started for it takes it; no "
                   "stand-still alert); normal: any worker")
    x.add_argument("--kind", choices=core.TASK_KINDS, help="operational: work on the operation of the system (a "
                   "cleanup, a change of the order, reading production logs to write a task); it has its own default "
                   "model and starts in a new session. work: any other task")
    x.add_argument("--mine", action="store_true",
                   help="you do this work now (a request in your chat, a note you write while you hold another "
                        "item): reserve it for you at once, so no other session starts it; claim or close it yourself")

    x = sub.add_parser("artifacts", help="what the sessions made or named: files they wrote, screenshots, canvases, local "
                       "pages, claude.ai artifact links (Sessions > Artifacts on the page); 'maxpm artifacts show <n>' reads one")
    x.add_argument("show", nargs="?", choices=["show"], help="show: print one row's text, or the path or link to read it")
    x.add_argument("ref", nargs="?", help="with show: the row number of the same listing, the row's id, or its path or URL")
    x.add_argument("--agent", help="of this session only, also one that ended long ago")
    x.add_argument("--item", type=int, help="what the sessions that held this item made while they held it")
    x.add_argument("--since", help="from this time on: 30d, 2h, or a date (2026-10-01)")
    x.add_argument("-n", "--limit", type=int, default=40, help="at most this many rows (default 40)")
    x = sub.add_parser("search", help="find items by words in their title, context, notes, and output: open, done, "
                       "and dropped, newest first (run it before maxpm add)")
    x.add_argument("words", nargs="+", help="every word must occur (a light stem: test finds tests)")
    x.add_argument("--project", help="only this project (a name or a comma list)")
    x.add_argument("--status", help="open, backlog, in_progress, held, done, dropped, closed, or all (default all)")
    x.add_argument("--since", help="created or closed since: 30d, 2h, or a date (2026-10-01)")
    x.add_argument("-n", "--limit", type=int, default=20, help="at most this many (default 20)")
    x.add_argument("--all-kinds", dest="kinds", action="store_true",
                   help="also deploy, review, and fixes items (default: work items only)")
    x = sub.add_parser("duplicates", help="groups of open items that look like the same work (the score of the "
                       "maxpm add check); merge them with maxpm merge")
    x.add_argument("--project", help="only this project (a name or a comma list)")
    x = sub.add_parser("merge", help="close an item as a duplicate of another: its notes, links, tags, messages, "
                       "and queue places move to the kept item")
    x.add_argument("keep", type=int, help="the item that stays"); x.add_argument("other", type=int, help="the duplicate")
    x.add_argument("--note", help="why they are the same work (required)")

    x = sub.add_parser("edit", help="change title, notes, doer, or project")
    x.add_argument("--tier", choices=core.TIERS, help="expert: a task that a person talks through with an expert "
                   "session (a session started for it takes it; a worker never does); test: the target session of a "
                   "live test, which waits at its prompt by design (a session started for it takes it; no "
                   "stand-still alert); normal: any worker")
    x.add_argument("--kind", choices=core.TASK_KINDS, help="operational: work on the operation of the system (a "
                   "cleanup, a change of the order, reading production logs to write a task); it has its own default "
                   "model and starts in a new session. work: any other task")
    x.add_argument("id", type=int); x.add_argument("--title"); x.add_argument("--notes")
    x.add_argument("--short", help="the short title every page shows: about 6 plain words, no ids, no file names, no "
                   "quotes; \"\" removes it")
    x.add_argument("--changed", help="what changed, one plain line for a person (no ids, no file names, no flags); "
                   "\"\" removes it")
    x.add_argument("--no-goal-context", dest="goal_context", action="store_false", default=None,
                   help="its session starts fresh, not as a fork of its goal's base")
    x.add_argument("--goal-context", dest="goal_context", action="store_true", help="undo --no-goal-context")
    x.add_argument("--doer", choices=core.DOERS); x.add_argument("--project")
    x.add_argument("--context"); x.add_argument("--touches", nargs="*", help="replaces the list; give none to clear it")
    x.add_argument("--check")
    _model_args(x, edit=True)
    x.add_argument("--due", help="due date (2026-10-15, 'fri 17:00'), or none to remove it")
    x.add_argument("--goal", action="append", help="tag the item with a goal (repeatable)")
    x.add_argument("--untag", action="append", help="remove a goal tag (repeatable)")
    _ref_args(x)
    x.add_argument("--unref", action="append", help="remove a tracker link (repeatable)")
    x.add_argument("--unreserve", action="store_true", help="end the reservation of an open item (\"reserved for <agent>\"), "
                   "so every agent can take it: that agent, a person, or a manager (the same as maxpm push <id> --cancel)")

    x = sub.add_parser("list", help="list items (open by default)")
    x.add_argument("--project"); x.add_argument("--status"); x.add_argument("--all", action="store_true")
    x.add_argument("--goal", help="only items tagged with this goal")
    x.add_argument("--ref", help="only items linked to this tracker issue, closed ones too (jira:PROJ-123)")

    x = sub.add_parser("show", help="one item with its links and history"); x.add_argument("id", type=int)
    x.add_argument("--brief", action="store_true", help="a few lines: the item, cut context and output, "
                   "open links only, no history")
    nt = sub.add_parser("notify", help="send needs-you notifications: run, test a channel, status")
    nts = nt.add_subparsers(dest="ncmd", required=True)
    x = nts.add_parser("run", help="send what is due (loops every notify_interval unless --once)")
    x.add_argument("--once", action="store_true", help="one pass, for launchd or cron")
    x.add_argument("--now", action="store_true", help="do not wait for the batch window")
    x = nts.add_parser("test", help="send a test message on one channel"); x.add_argument("channel")
    nts.add_parser("status", help="per channel: configured, pending, last send, last error")
    x = nts.add_parser("setup", help="set up a channel (ntfy: makes a secret topic and prints the phone steps)")
    x.add_argument("channel", choices=["ntfy"]); x.add_argument("--url", help="ntfy server (default https://ntfy.sh)")
    x.add_argument("--token", help="access token for a protected or self-hosted ntfy server")
    x.add_argument("--open", choices=["maxpm", "agent"], help="where a tap opens (notify_open, for you): "
                   "the MaximizePM page, or the agent's own app (claude.ai/code, chatgpt.com/codex)")
    x = sub.add_parser("prompt", help="a paste-ready prompt for an agent that helps a person with a human item")
    x.add_argument("id", type=int, nargs="?"); x.add_argument("--all", action="store_true", help="every item and question that waits on the person")
    x.add_argument("--for", dest="person", help="the person (default: you if you are a person, else the first registered person)")
    x = sub.add_parser("needs-you", help="what waits on a person, most important first: questions and alerts, then ready human items by priority")
    x.add_argument("--human", help="only this person's (and those for anyone)"); x.add_argument("--all", action="store_true", help="closed ones too")
    x = sub.add_parser("status", help="overview: every project's counts, recent completions, who is working, open slots")
    x.add_argument("--recent", type=int, default=10, help="how many recent completions (default 10)")
    x = sub.add_parser("usage", help="token cost of each done item, from its Claude Code sessions' transcripts, "
                       "and the medians for fresh and forked sessions")
    x.add_argument("--project"); x.add_argument("--since", default="7d", help="how far back (default 7d; all for everything)")
    x.add_argument("--measure", type=int, metavar="ID", help="read this item's usage from the transcripts again first")
    x.add_argument("--archive", action="store_true", help="the archived items too (slower)")
    x.add_argument("--limit", "-n", type=int, default=20, help="how many items to list (default 20)")
    x = sub.add_parser("health", help="does MaximizePM work: person minutes per done item, items done per day, the "
                       "rework rate, the person's waits, session idle time and cost, against the time before")
    x.add_argument("--project"); x.add_argument("--since", default="7d", help="how far back (default 7d; 30d; all)")
    x.add_argument("--until", help="the end of the time (default now)")
    x.add_argument("--by", choices=["goal"], help="goal: items with a goal tag against items without one, per project")
    x.add_argument("--archive", action="store_true", help="the archived items too (slower)")
    x = sub.add_parser("change", help="the change log of maxpm health: when a change to the way of work shipped")
    chs = x.add_subparsers(dest="chcmd", required=True)
    y = chs.add_parser("add", help="record a change (a person or the manager)")
    y.add_argument("name"); y.add_argument("--at", help="when it shipped (default now): 2026-11-10, '2026-11-10 14:00'")
    y.add_argument("--note", default="")
    chs.add_parser("list", help="the change log, oldest first")
    y = chs.add_parser("remove", help="take a wrong row out (a person or the manager)")
    y.add_argument("id")
    x = sub.add_parser("log", help="completed work: done items with output, who, and when, by day")
    x.add_argument("--project"); x.add_argument("--since", default="7d", help="how far back (default 7d; all for everything)")

    x = sub.add_parser("next", help="the next ready item from the area you choose")
    x.add_argument("--project", help="one project, or a comma list")
    x.add_argument("--unblocks", help="prerequisites of this item id or project")
    x.add_argument("--near", help="items linked to these item ids (comma list), closest first")
    x.add_argument("--mine", action="store_true", help="items linked to what you claimed or finished before, then your projects")
    x.add_argument("--claim", action="store_true", help="take it")
    x.add_argument("--limit", "-n", type=int, default=1)
    x.add_argument("--model", default=os.environ.get("MAXPM_MODEL"),
                   help="the model this session runs (default $MAXPM_MODEL): skip items whose limits exclude it")

    x = sub.add_parser("init", help="set up the current folder: link or create its project, add the agent block")
    x.add_argument("--project", help="project name (default: the folder name)")
    x.add_argument("--description", default="", help="what the project covers, for agents")
    x.add_argument("--file", action="append", help="add the block to this file only (repeatable)")
    x.add_argument("--move", action="store_true", default=None,
                   help="move the rules in CLAUDE.md to AGENTS.md; CLAUDE.md becomes @AGENTS.md")
    x.add_argument("--no-move", dest="move", action="store_false", help="keep CLAUDE.md and AGENTS.md apart")
    x.add_argument("--tracker", help="the outside tracker it uses: \"github owner/repo via gh\", \"jira PROJ via the Jira MCP server\"")

    x = sub.add_parser("go", help="start or continue an agent session: name, role, item, briefing")
    x.add_argument("--project", help="project name(s) when this folder is not linked")
    x.add_argument("--role", choices=core.ROLES, help="ask for a role instead of letting MaximizePM pick")
    x.add_argument("--session", help="your Claude Code session name, so others can message this session")
    x.add_argument("--model", default=os.environ.get("MAXPM_MODEL"),
                   help="the model this session runs (default $MAXPM_MODEL): MaximizePM gives it only items its model is allowed for")
    x.add_argument("--chat", action="store_true", default=os.environ.get("MAXPM_CHAT") == "1",
                   help="a chat app session with no folder or shell (default $MAXPM_CHAT=1): every project, only "
                        "items that need no folder, the result in done --output")

    x = sub.add_parser("manage", help="start the manager session (one at a time): what needs attention, and its rules")
    x.add_argument("--takeover", metavar="REASON", help="take over from the active manager")
    x.add_argument("--watch", action="store_true", help="block until a new finding or a message needs the manager (at most manage_every); run it in the background")
    x.add_argument("--step", help="with --watch: return after this long at most (a foreground shell: below its time limit)")
    x.add_argument("--chat", action="store_true", default=os.environ.get("MAXPM_CHAT") == "1",
                   help="a chat app session with no folder (default $MAXPM_CHAT=1)")
    x = sub.add_parser("plan", help="the planner role is gone (#4978): says where planning happens now")
    x.add_argument("--project", help="project name(s) to focus on (default: this folder's, else all)")
    x.add_argument("--chat", action="store_true", default=os.environ.get("MAXPM_CHAT") == "1",
                   help="a chat app session with no folder (default $MAXPM_CHAT=1)")

    x = sub.add_parser("launch", help="start an agent session in a new pane of the tmux session maxpm, like the page's Start")
    g = x.add_mutually_exclusive_group()
    g.add_argument("--project", help="its most important ready item, a release review too "
                                     "(default: first a project with no agent yet)")
    g.add_argument("--item", type=int, help="this ready item (Dispatch); for a release review, a reviewer in a "
                                            "folder of the release that wrote none of it")
    x.add_argument("--agent", help="a launch_agents label (default: the first)")
    x.add_argument("--model", help="model for the session (the item's limits apply)")
    x.add_argument("--effort", help="effort level for the session")
    x.add_argument("--option", action="append", default=[], metavar="NAME=VALUE",
                   help="a launch profile option for this session, e.g. remote_control=off, permission_mode=plan, "
                        "sandbox=read-only (repeatable)")
    x.add_argument("--prompt", metavar="TEXT",
                   help="the session's first instruction, after the profile's prompt (go) and a blank line, so the "
                        "session still runs go and registers; at most %d characters. It always opens a new session "
                        "(a session that waits for work would get it only as a message)" % core.CUSTOM_PROMPT_MAX)
    x.add_argument("--dry-run", action="store_true", help="say what it would start; open nothing")
    x = sub.add_parser("expert", help="expert sessions: a session the person talks with for hours or days; it takes no "
                       "queue work, and MaximizePM never ends it as it ends a worker")
    xs = x.add_subparsers(dest="ecmd")
    y = xs.add_parser("start", help="open an expert session on a topic in a new pane of the tmux session maxpm")
    y.add_argument("name", help="its name (letters, digits, '.', '_', '-')")
    y.add_argument("--topic", help="what it knows and what you ask it about: the title of the expert-tier task it adds")
    y.add_argument("--item", type=int, help="take this task (it becomes expert-tier) instead of adding one")
    y.add_argument("--summary", help="the talk so far, when you hand a talk over to the expert: what was said, what "
                   "is decided, what is open; it goes into the task's context (the manager must give it for a new topic)")
    y.add_argument("--summary-file", help="the summary from a file ('-': stdin)")
    y.add_argument("--source", action="append", default=[], help="a file, URL, or item the talk used (repeatable)")
    y.add_argument("--canvas", action="append", default=[], help="a canvas the expert reads (repeatable)")
    y.add_argument("--session", action="append", default=[], help="a session whose talk the expert reads, often your "
                   "own name; its transcript is named in the task (repeatable)")
    y.add_argument("--project", help="the project whose folder it works in (default: its last one, else the first "
                                     "project with a folder)")
    y.add_argument("--resume", action="store_true", help="resume its last Claude Code chat, with its context")
    y.add_argument("--agent", help="a launch_agents label (default: the first)")
    y.add_argument("--model", help="its model (your choice: fable too)")
    y.add_argument("--effort", help="its effort level")
    y = xs.add_parser("propose", help="the manager proposes an expert for a long talk: the person's chat with you shows "
                      "a card, Continue in an expert or Dismiss; nothing starts until the person clicks")
    y.add_argument("name", help="the expert's name (letters, digits, '.', '_', '-')")
    y.add_argument("--topic", help="what the talk is about: the title of the expert-tier task (#<id> names the work "
                   "it continues, and gives its model)")
    y.add_argument("--item", type=int, help="the task it takes, instead of a new one")
    y.add_argument("--summary", help="the talk so far: what was said, what is decided, what is open")
    y.add_argument("--summary-file", help="the summary from a file ('-': stdin)")
    y.add_argument("--source", action="append", default=[], help="a file, URL, or item the talk used (repeatable)")
    y.add_argument("--canvas", action="append", default=[], help="a canvas the expert reads (repeatable)")
    y.add_argument("--session", action="append", default=[], help="a session whose talk the expert reads (repeatable)")
    y.add_argument("--project", help="the project whose folder it works in")
    y.add_argument("--model", help="its model; else the model of the work it continues")
    y.add_argument("--effort", help="its effort level")
    y = xs.add_parser("request", help="ask for an expert (a person asked you for one): adds the expert-tier task and "
                      "alerts the manager with the line that starts it as an expert")
    y.add_argument("topic", help="what the expert does: the title of its task")
    y.add_argument("--model", help="its model (the person's word); else the model of the work it continues")
    y.add_argument("--for", dest="person", help="the person who talks with the expert")
    y.add_argument("--name", help="the expert's name (default: <project>-expert)")
    y.add_argument("--context", help="what it reads, the rules it keeps, where its output goes")
    y.add_argument("--item", type=int, help="the work it continues (default: the item you hold)")
    y.add_argument("--project", help="its project (default: that of the item, else of this folder)")
    y.add_argument("--blocked", action="store_true", help="you cannot go on until the expert answers: the manager "
                   "wakes at once")
    xs.add_parser("proposals", help="the open expert proposals")
    y = xs.add_parser("accept", help="start a proposed expert (the page's Continue in an expert)")
    y.add_argument("id", help="the proposal (p<id>)")
    y = xs.add_parser("dismiss", help="dismiss a proposed expert: the manager continues the talk itself")
    y.add_argument("id", help="the proposal (p<id>)")
    y.add_argument("--why", help="why (the manager hears it)")
    y = xs.add_parser("adopt", help="make this session an expert: the task it holds becomes expert-tier (run it in the "
                      "session, with --as <its name>)")
    y.add_argument("--topic", help="with no task in hand: add an expert-tier task with this title and take it")
    y.add_argument("--project", help="its project (default: the project of this folder)")
    y = xs.add_parser("end", help="end an expert (a person, the manager, or the expert itself)")
    y.add_argument("name")
    y.add_argument("--reason", help="why (its unread senders hear it)")
    y.add_argument("--done", action="store_true", help="the talk is over: its expert-tier task is done (else it goes "
                   "back to the queue, and expert start --resume takes it again)")
    xs.add_parser("list", help="the experts: topic, model, last talk, when they end, their prompt cache")
    y = xs.add_parser("fresh", help="the expert's context left the prompt cache: end it, and start a fresh expert on "
                      "its task and model that reads only its handoff (#5023)")
    y.add_argument("name")
    y.add_argument("--new", metavar="NAME", help="the fresh expert's name (default: <name>-2, or the next number)")
    y = xs.add_parser("notes", help="an expert's notes (a canvas): read them, or --put a new version")
    y.add_argument("name")
    y.add_argument("--put", metavar="FILE", help="store a new version from a file ('-' reads stdin)")

    x = sub.add_parser("view", help="show every agent that runs in tmux side by side in this terminal")
    g = x.add_mutually_exclusive_group()
    g.add_argument("--windows", dest="layout", action="store_const", const="windows", default="tile",
                   help="one tmux window for each agent, not side by side")
    g.add_argument("--list", dest="layout", action="store_const", const=None,
                   help="print the agents' panes; change and show nothing")
    x.add_argument("--tidy", action="store_true",
                   help="first close the panes of sessions that are done: the agent CLI ended, or it stays open but "
                        "its agent left the queue, stopped, or is gone, and holds nothing; never a pane that shows a prompt")
    x.add_argument("--orphans", action="store_true",
                   help="list the tmux servers the tests left behind with their socket gone (only shells in their "
                        "panes; never the server of the agents); with --tidy, end them. maxpm serve ends them every tidy_every")
    x = sub.add_parser("stop", help="ask an agent to stop: it commits, releases its item, and ends (not a kill)")
    x.add_argument("agent"); x.add_argument("--reason", required=True, help="why; the agent sees it")
    x.add_argument("--kill", action="store_true",
                   help="emergency only: end its process on this host now and release what it holds (uncommitted work is lost)")
    x = sub.add_parser("interrupt", help="end an agent's running turn now, as Esc in its terminal (it keeps its session "
                                         "and item); --message then types a message into it at once (a person, or the manager)")
    x.add_argument("agent"); x.add_argument("--message", help="the text the agent reads at once, after its turn ended")
    q = sub.add_parser("queue", help="one agent's own ordered queue: items it takes first, and instructions")
    qs = q.add_subparsers(dest="qcmd", required=True)
    x = qs.add_parser("add", help="add an item (at the end, --first, or --before <id>) or an instruction (--message); a push of the item to another agent ends, and that agent hears it")
    x.add_argument("agent"); x.add_argument("id", type=int, nargs="?")
    x.add_argument("--message", help="an instruction the agent reads first, at the top of its maxpm go")
    x.add_argument("--first", action="store_true"); x.add_argument("--before", type=int)
    x = qs.add_parser("list", help="an agent's queue (default: yours)"); x.add_argument("agent", nargs="?")
    x = qs.add_parser("remove", help="take an item (12) or an instruction (e5) out of a queue")
    x.add_argument("agent"); x.add_argument("ref")
    x = qs.add_parser("move", help="move an item in a queue"); x.add_argument("agent"); x.add_argument("id", type=int)
    x.add_argument("--before", type=int); x.add_argument("--after", type=int)
    x = sub.add_parser("claim", help="take one ready item by id"); x.add_argument("id", type=int)
    x = sub.add_parser("done", help="finish an item"); x.add_argument("id", type=int); x.add_argument("--output")
    x.add_argument("--changed", help="what changed, one plain line for a person: no ids, no file names, no flags "
                   "(the pages show it; --output stays the note for agents)")
    x.add_argument("--ship", action="store_true", help="also ask for it to be deployed (maxpm ship)")
    x.add_argument("--note", help="why an agent may close a person's item (required then; the user is told)")
    x.add_argument("--synced", action="store_true", help="you already posted the result to its tracker issues")
    x.add_argument("--force", metavar="REASON", help="close it although items it waits on are still open (not a deploy)")
    x = sub.add_parser("wait", help="no work now: block until a push, a ready item, or a message comes (up to wait_step); "
                       "a deployer or reviewer session (MAXPM_FOCUS deploy:/review:) waits only for a deploy or a review")
    x.add_argument("--project", help="project name(s) to wait on (default: this folder's)")
    x.add_argument("--step", help="return after this long (default: the wait_step setting)")
    x = sub.add_parser("cleanup", help="open items that may be done or stale: expired leases, commits that name them, ...")
    x.add_argument("--project"); x.add_argument("--no-git", action="store_true", help="skip the git log checks")
    x.add_argument("--space", action="store_true", help="the size limits instead (db_max_size, folder_max_size): "
                   "remove the oldest removable data now, until each size is under its limit")
    x.add_argument("--dry-run", action="store_true", help="with --space (or alone): say what would go, remove nothing")
    x.add_argument("--closes", metavar="SINCE", help="first run the close check on the closes of this time (7d) that "
                   "it has not checked, such as closes before the check existed")
    x = sub.add_parser("check", help="record what a check of a suspect item found: done, partial, or open")
    x.add_argument("id", type=int); x.add_argument("result", choices=core.CHECK_RESULTS)
    x.add_argument("--note", help="where the work is (done), what is left (partial), or what you looked at (open)")
    x = sub.add_parser("synced", help="record that a done item's tracker issues got the result (comment, close)")
    x.add_argument("id", type=int); x.add_argument("--ref", help="only this link (default: all of the item's links)")
    x = sub.add_parser("ship", help="ask for an item to be deployed: it joins its target's next deploy item")
    x.add_argument("id", type=int)
    rv = sub.add_parser("review", help="release reviews: pass one, or send the release back with fixes")
    rvs = rv.add_subparsers(dest="rcmd", required=True)
    x = rvs.add_parser("pass", help="accept the release (runs the command steps and review_cmd first)"); x.add_argument("id", type=int)
    x.add_argument("--output", help="what you checked; default: review passed")
    x.add_argument("--confirm", action="append", metavar="all|IDS",
                   help="the written review steps you did: all, or step ids (3,5); required when the release has any")
    x.add_argument("--rev", help="the commit you reviewed; the release's git folder must hold it after the steps, "
                   "and a release with no pin is pinned at it (default: the folder's HEAD after the steps)")
    x = rvs.add_parser("fail", help="add fix items the review waits on, and release the review")
    x.add_argument("id", type=int); x.add_argument("fixes", nargs="+", help="one title per fix item")
    x.add_argument("--note", help="what the review found (goes into each fix item's context)")
    x.add_argument("--project", help="project for the fix items (default: of the first item the review covers)")
    x.add_argument("--ask", action="store_true",
                   help="ask the user first: one item for a person lists the proposed fixes; done adds them")
    st = rvs.add_parser("step", help="the review steps of a project: what a release review of it follows")
    sts = st.add_subparsers(dest="scmd", required=True)
    x = sts.add_parser("list", help="the steps of one project, or of all"); x.add_argument("project", nargs="?")
    x = sts.add_parser("add", help="add a step: a written instruction, or with --run a command that must exit 0")
    x.add_argument("project"); x.add_argument("text", help="the instruction, or the command with --run")
    x.add_argument("--run", action="store_true", help="the text is a command, run in the project folder")
    x.add_argument("--at", type=int, help="position (1 is first; default: last)")
    x = sts.add_parser("edit", help="change a step's text or kind"); x.add_argument("id", type=int)
    x.add_argument("--text"); k = x.add_mutually_exclusive_group()
    k.add_argument("--run", dest="run", action="store_true", default=None, help="make it a command")
    k.add_argument("--do", dest="run", action="store_false", help="make it a written instruction")
    x = sts.add_parser("rm", help="remove a step"); x.add_argument("id", type=int)
    x = sts.add_parser("move", help="move a step within its project"); x.add_argument("id", type=int)
    x.add_argument("to", type=int, help="new position (1 is first)")
    po = sub.add_parser("policy", help="the rules sessions follow, each with its source and reason: global ones ship "
                        "with MaximizePM, user ones are yours (queue, project or folder, role); the briefings print the ones "
                        "that apply")
    pos = po.add_subparsers(dest="polcmd", required=True)
    x = pos.add_parser("list", help="every policy, or those of one project or role")
    x.add_argument("--project"); x.add_argument("--role", help="worker, reviewer, deployer, owner, manager, expert")
    x.add_argument("--for", dest="for_role", metavar="ROLE",
                   help="what applies to a session of this role (with --project: in that project)")
    k = x.add_mutually_exclusive_group()
    k.add_argument("--global", dest="kind", action="store_const", const="global", help="only the global policies")
    k.add_argument("--user", dest="kind", action="store_const", const="user", help="only your own policies")
    x = pos.add_parser("show", help="one policy in full"); x.add_argument("id", help="p12 (yours) or g3 (global)")
    x = pos.add_parser("add", help="add your own rule: short text, its reason and source, and a scope (default: the "
                       "whole queue)")
    x.add_argument("text", help="the rule, in one or two plain sentences")
    x.add_argument("--reason", required=True, help="why the rule exists")
    x.add_argument("--source", help="who decided it, and when: 'mark 2026-11-11' (default: you, today)")
    x.add_argument("--project", help="only sessions in this project")
    x.add_argument("--folder", help="only sessions in this folder: every project of it (in place of --project)")
    x.add_argument("--role", help="only this role: " + ", ".join(core.POLICY_ROLES))
    x.add_argument("--item", action="append", metavar="ID", help="an item the rule came from (repeatable)")
    x.add_argument("--adds-to", metavar="G", help="the global policy (g3) this rule adds to")
    x = pos.add_parser("edit", help="change a policy's text, reason, source, scope or items"); x.add_argument("id")
    x.add_argument("--text"); x.add_argument("--reason"); x.add_argument("--source")
    x.add_argument("--project"); x.add_argument("--no-project", action="store_true", help="every project")
    x.add_argument("--folder", help="every session in this folder (replaces the project)")
    x.add_argument("--no-folder", action="store_true", help="no longer only one folder")
    x.add_argument("--role", help="a role, or any")
    x.add_argument("--item", action="append", metavar="ID", help="replace the item links (--item '' clears them)")
    x.add_argument("--adds-to", metavar="G", help="the global policy it adds to (none: no longer)")
    x = pos.add_parser("retire", help="delete a policy (no tombstone; the history keeps one line)"); x.add_argument("id")
    x = sub.add_parser("release", help="give a claimed item back; with no item, an agent's hook says that its session "
                       "ended (docs/agent-adapters.md)")
    x.add_argument("id", type=int, nargs="?", help="the item (none: the session of --as ended, and it is gone)")
    x.add_argument("--note")
    x.add_argument("--author", metavar="COMMITS", help="a release review only: you wrote commits of the release (say "
                   "which); maxpm go and maxpm serve give you this review no more, and another session takes it")
    x = sub.add_parser("drop", help="close an item without doing it"); x.add_argument("id", type=int)
    x.add_argument("--note", help="why (required when an agent drops a person's item)")
    x = sub.add_parser("takeover", help="an agent does a person's item itself (the user is told, and can undo)")
    x.add_argument("id", type=int); x.add_argument("--note", required=True, help="how you will do it without the user")
    x = sub.add_parser("undo-takeover", help="give a taken-over item back to the people"); x.add_argument("id", type=int)
    x = sub.add_parser("reopen", help="open a closed item again"); x.add_argument("id", type=int)
    x = sub.add_parser("backlog", help="move an open item to the backlog (kept, never offered), or list the backlog")
    x.add_argument("id", type=int, nargs="?", help="the item to move; without it, list the backlog")
    x.add_argument("--note", help="why nobody takes it now (required with an id)")
    x.add_argument("--force", metavar="WHY", help="move it although open items wait on it: say why they may wait")
    x.add_argument("--project", help="list: only this project (a name or a comma list)")
    x = sub.add_parser("activate", help="take an item out of the backlog: open again, offered when ready")
    x.add_argument("id", type=int); x.add_argument("--note", help="why now")
    x = sub.add_parser("prio", help="set item priority"); x.add_argument("id", type=int); x.add_argument("priority", type=int)
    x = sub.add_parser("move", help="manual order inside a project")
    x.add_argument("id", type=int); g = x.add_mutually_exclusive_group(required=True)
    g.add_argument("--before", type=int); g.add_argument("--after", type=int)
    x = sub.add_parser("dep", help="make an item wait on others, or mark items that must not run together")
    x.add_argument("id", type=int); x.add_argument("--on", type=int, nargs="+", required=True)
    x.add_argument("--kind", default="blocks", choices=core.DEP_KINDS,
                   help="blocks: wait for it (default); feeds: wait, then read its output; "
                        "conflicts: no order, never in progress together")
    g = x.add_mutually_exclusive_group()
    g.add_argument("--keep", dest="mode", action="store_const", const="keep",
                   help="you hold <id>: keep it and do the prerequisites yourself")
    g.add_argument("--release", dest="mode", action="store_const", const="release",
                   help="you hold <id>: give it back while the prerequisites wait")
    x = sub.add_parser("push", help="reserve an open item for one agent and alert it")
    x.add_argument("id", type=int); x.add_argument("--to"); x.add_argument("--note")
    x.add_argument("--cancel", action="store_true", help="take back the open push of this item (a person or a manager: any reservation)")
    x = sub.add_parser("accept", help="take an item pushed to you, or with --message say yes to an alert")
    x.add_argument("id", type=int)
    x.add_argument("--message", action="store_true", help="the id is an alert: claim its item now, or keep it for after your current item")
    for kind, verb in (("alert", "tell an agent about work it probably needs to do"),
                       ("ask", "ask an agent a question")):
        x = sub.add_parser(kind, help=f"{verb}: maxpm {kind} [<agent>] \"...\" [--holder-of <id>] [--item <id>]"
                           + (" [--file <path>]" if kind == "ask" else ""))
        x.add_argument("words", nargs="+", metavar="[agent] text")
        x.add_argument("--holder-of", type=int, help="send it to whoever holds this item")
        x.add_argument("--item", type=int, help="the item it is about (without an agent: its holder)")
        x.add_argument("--goal", help="send it to the owner of this goal")
        x.add_argument("--level", metavar="|".join(core.MESSAGE_LEVELS), help=LEVEL_HELP)
        x.add_argument("--blocked", action="store_true", help=BLOCKED_HELP)
        if kind == "ask":
            x.add_argument("--file", help="ask every agent whose held items touch this file")
    x = sub.add_parser("decline", help="hand a pushed item back, or with --message say no to an offer or alert")
    x.add_argument("id", type=int); x.add_argument("--note")
    x.add_argument("--message", action="store_true", help="the id is a message (an offer or alert), not an item")
    x = sub.add_parser("offer", help="offer help to the agent that holds an item you are blocked on")
    x.add_argument("text"); x.add_argument("--item", type=int, required=True); x.add_argument("--to", help="default: its holder")
    x.add_argument("--goal", help="send the offer to the owner of this goal instead")
    x = sub.add_parser("give", help="hand an item you hold (or that is reserved for you) to another agent")
    x.add_argument("id", type=int); x.add_argument("--to", required=True)
    x = sub.add_parser("split", help="add smaller prerequisites anyone can take; your item waits for them, still yours")
    x.add_argument("id", type=int); x.add_argument("titles", nargs="+"); x.add_argument("--doer", default="any", choices=core.DOERS)
    x = sub.add_parser("keep", help="hold an item again while you do its open prerequisites; with --close, keep an "
                       "item that the close check of an item listed (it is still needed)")
    x.add_argument("id", type=int, nargs="?", help="the item (with --close and no id: every item that check lists)")
    x.add_argument("--close", type=int, metavar="CLOSED_ID", help="the closed item whose close check listed it")
    x.add_argument("--note", help="with --close: why it is still needed")
    x = sub.add_parser("undep", help="remove waits or conflict links"); x.add_argument("id", type=int); x.add_argument("--on", type=int, nargs="+", required=True)
    x = sub.add_parser("blocked", help="record a blocker outside the queue, optionally until a time")
    x.add_argument("id", type=int); x.add_argument("--reason")
    x.add_argument("--until", help="it ends by itself then: 2h, 2026-09-28T07:00, or 'mon 07:00 America/New_York'")
    x = sub.add_parser("unblock", help="clear an outside blocker"); x.add_argument("id", type=int)
    x = sub.add_parser("replanned", help="clear the replan mark after you planned the item again")
    x.add_argument("id", type=int); x.add_argument("--note")
    x = sub.add_parser("blockers", help="tree of what an item waits on"); x.add_argument("id", type=int)

    x = sub.add_parser("send", help="send an alert, question, or note to an agent or to the holder of an item")
    x.add_argument("kind", choices=core.SEND_KINDS); x.add_argument("text")
    x.add_argument("--to", help="agent name"); x.add_argument("--item", type=int, help="the item it is about; without --to it goes to the holder")
    x.add_argument("--reply", type=int, help="message id this replies to (goes to its sender)")
    x.add_argument("--goal", help="send it to the owner of this goal")
    x.add_argument("--level", metavar="|".join(core.MESSAGE_LEVELS), help=LEVEL_HELP)
    x.add_argument("--blocked", action="store_true", help=BLOCKED_HELP)
    x = sub.add_parser("answer", help="answer a question"); x.add_argument("id", type=int); x.add_argument("text")
    x.add_argument("--in-chat", action="store_true", help="you asked it, and the person answered in your own chat: "
                   "record their words as the answer and close the question")
    x.add_argument("--self", dest="self_answer", action="store_true", help="you asked it, and you have the answer "
                   "from any channel (a message, a canvas card, your chat): record it and close the question")
    x = sub.add_parser("forward", help="the manager: send a question, alert or person item that waits for you on to the person")
    x.add_argument("id", type=int, nargs="?", help="a held question or alert (message id)")
    x.add_argument("--item", type=int, help="a held person item (item id) instead of a message")
    x.add_argument("--drop", action="store_true", help="a held alert you dealt with yourself: close it, the person never sees it")
    x.add_argument("--note", help="what you add for the person (what you checked, your recommendation)")
    x = sub.add_parser("withdraw", help="close a question you asked that is moot now (the work answered it)")
    x.add_argument("id", type=int)
    x.add_argument("--note", required=True, help="why it is moot")
    x = sub.add_parser("inbox", help="your unread messages and questions waiting for your answer")
    x.add_argument("--all", action="store_true", help="read messages too")
    x.add_argument("--peek", action="store_true", help="do not mark them read")
    x.add_argument("--wait", action="store_true",
                   help="block until a new message comes, then print it; run it as a background command")
    x.add_argument("--timeout", help=f"with --wait: return after this long with nothing new (default {core.INBOX_WAIT})")
    x = sub.add_parser("thread", help="a message and its replies")
    x.add_argument("id", type=int, nargs="?"); x.add_argument("--item", type=int, help="every message about this item")

    x = sub.add_parser("register", help="register this agent or person")
    x.add_argument("name"); x.add_argument("--human", action="store_true"); x.add_argument("--note", default="")
    x.add_argument("--session", help="the Claude Code session this agent runs in")
    x = sub.add_parser("session", help="record the Claude Code session you run in: maxpm session \"<name>\" --ref <ref>; "
                       "or pin a session to the top of the Sessions tab: maxpm --as <you> session pin|unpin <agent>")
    x.add_argument("name", nargs="*", help="the name ListAgents prints; put it in quotes when it has spaces or starts with #")
    x.add_argument("--ref", help="the short code in brackets after the name in ListAgents")
    x.add_argument("--move", metavar="SESSION_ID", help="a person or the manager: the session id belongs to --to only")
    x.add_argument("--to", metavar="AGENT", help="with --move: the agent whose session it is")
    x = sub.add_parser("unregister", help="remove an agent that holds nothing"); x.add_argument("name")
    x = sub.add_parser("now", help="what you do now, in about 10 plain words, for your row on the Sessions tab: "
                       "run it when you start an item and at each new step")
    x.add_argument("words", nargs="*", metavar="text", help="what you do now, for example: Runs the tests after the fix")
    x.add_argument("--work", help="a short name of your work (about 6 words), when you hold no item or its short title "
                   "does not say it")
    x = sub.add_parser("note", help="set your status note; with an agent, --holder-of, or --item: send a note")
    x.add_argument("words", nargs="+", metavar="[agent] text")
    x.add_argument("--holder-of", type=int, help="send the note to whoever holds this item")
    x.add_argument("--item", type=int, help="the item it is about (without an agent: its holder)")
    x.add_argument("--goal", help="send the note to the owner of this goal")
    x.add_argument("--level", metavar="|".join(core.MESSAGE_LEVELS), help=LEVEL_HELP)
    x.add_argument("--blocked", action="store_true", help=BLOCKED_HELP)
    x = sub.add_parser("who", help="who is doing what"); x.add_argument("--item", type=int); x.add_argument("--project")
    x.add_argument("--all", action="store_true", help="also stopped and gone agents that hold nothing")
    x.add_argument("--file", help="only agents whose held items touch this file or directory")
    sub.add_parser("heartbeat", help="renew your leases")
    x = sub.add_parser("report", help="for an agent's hook, plugin or pane rule: the session's state now "
                                      "(docs/agent-adapters.md); maxpm release says that it ended")
    x.add_argument("--state", choices=core.REPORT_STATES,
                   help="working: it runs a turn; idle: it waits at its prompt; blocked: it waits for a person")
    x.add_argument("--hook", metavar="AGENT", help="for an agent's hook that passes its own input on stdin (Claude "
                   "Code's JSON): the adapter of AGENT reads it; in place of --state")
    x.add_argument("--message", help="blocked: what it waits for (a permission menu, a question)")
    x.add_argument("--session-id", dest="session_id", help="the agent's own id of the session")
    x.add_argument("--seq", type=int, help="a number that grows with each report: a late report changes nothing")
    x.add_argument("resume", nargs=argparse.REMAINDER, metavar="-- RESUME",
                   help="after --: the command that resumes this session")
    sub.add_parser("capacity", help="how many agent sessions the graph can use now")

    c = sub.add_parser("config", help="settings")
    cs = c.add_subparsers(dest="ccmd", required=True)
    for name in ("get", "set", "unset"):
        y = cs.add_parser(name)
        if name != "get":
            y.add_argument("key")
        else:
            y.add_argument("key", nargs="?")
        if name == "set":
            y.add_argument("value")
        y.add_argument("--project"); y.add_argument("--item", type=int); y.add_argument("--agent")
        y.add_argument("--kind", help="items of one kind (work, deploy, review): for the default_* model settings")

    x = sub.add_parser("serve", help="the web page on 127.0.0.1")
    x.add_argument("--port", type=int); x.add_argument("--open", action="store_true")
    x.add_argument("--dev", action="store_true", help="restart on code change; the page reloads itself")
    x.add_argument("--restart", action="store_true",
                   help="ask the maxpm serve that runs to start again with the code on disk, and wait until it answers")
    x.add_argument("--install-login", action="store_true",
                   help="start maxpm serve at login and again after a crash (macOS: a launchd agent; Linux: a systemd "
                        "user unit), for a computer you reach through the relay")
    x.add_argument("--remove-login", action="store_true", help="remove that login agent")
    x.add_argument("--kick", action="store_true", help="stop maxpm serve and start it again through its login agent, "
                   "or through the open desktop app, also when it hangs (the repair of the serve check)")
    x.add_argument("--install-watchdog", action="store_true",
                   help="a job that runs maxpm ops check --outside every minute, also when maxpm serve is down (the "
                        "checks marked outside, their repairs, a notice); macOS: launchd, Linux: a systemd timer")
    x.add_argument("--remove-watchdog", action="store_true", help="remove that job")
    sk = sub.add_parser("skills", help="install the agent guides as Claude Code skills")
    sks = sk.add_subparsers(dest="scmd", required=True)
    x = sks.add_parser("install", help="link the guides (maxpm, maxpm-manager) into ~/.claude/skills")
    x.add_argument("--dir", default="~/.claude/skills", help="skills folder (default ~/.claude/skills)")
    x.add_argument("--copy", action="store_true", help="copy the files instead of linking them")
    x.add_argument("--force", action="store_true", help="replace a folder that is not a link (your edits there are lost)")
    sub.add_parser("activity", help="the old name of maxpm report --hook claude (the hook files of sessions that "
                                    "started before #5183 run it)")
    db = sub.add_parser("db", help="where the queue database is")
    dbs = db.add_subparsers(dest="dcmd", required=True)
    dbs.add_parser("path", help="print the database file MaximizePM uses")
    sub.add_parser("mcp", help="an MCP server on stdin/stdout, for agents that cannot run shell commands")
    x = sub.add_parser("connect", help="connect this computer to the MaximizePM relay, so that you can open this "
                                       "MaximizePM's page from any browser (maxpm serve keeps the connection open)")
    x.add_argument("--off", action="store_true", help="disconnect: revoke this computer's relay token and forget it")
    x.add_argument("--status", action="store_true", help="show the relay connection")
    x.add_argument("--replace", action="store_true",
                   help="take over the relay account from another computer that is connected to it now")
    x.add_argument("--relay", metavar="URL", help="the relay (default https://relay.maximizepm.com, or $MAXPM_RELAY_URL)")
    x.add_argument("--no-browser", action="store_true", help="print the link instead of opening the browser")
    x = sub.add_parser("guide", help="how to use MaximizePM: worker loop, manager (and writing tasks), or agent setup")
    x.add_argument("which", nargs="?", default="worker",
                   choices=["worker", "manager", "setup", "decisions"])
    x = sub.add_parser("setup-agent", help="print (or append) the instructions block for CLAUDE.md / AGENTS.md")
    x.add_argument("--append", metavar="FILE", help="append the block to this file if it is not there yet")
    x.add_argument("--claude-desktop", action="store_true",
                   help="add MaximizePM (maxpm mcp) to the Claude desktop app's MCP servers, leaving the others as they are")
    x.add_argument("--chatgpt-desktop", "--codex", dest="codex", action="store_true",
                   help="add MaximizePM (maxpm mcp) to ~/.codex/config.toml, which the ChatGPT desktop app (Work and Codex "
                        "modes) and the Codex CLI share, leaving the other servers as they are")
    x.add_argument("--remove", action="store_true", help="with --claude-desktop or --chatgpt-desktop: take MaximizePM out again")
    x.add_argument("--config", help="with --claude-desktop or --chatgpt-desktop: the config file (default: where the app keeps it)")
    return p


def install_skills(dest, copy=False, force=False):
    """Link (or copy) the packaged guides into a skills folder. Returns one line per skill."""
    import shutil
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    gone = dest / "maxpm-planner"  # the planner guide is gone (#4978): a link to it would point at nothing
    if gone.is_symlink():
        gone.unlink()
        out.append(f"removed {gone} (the planner role is gone)")
    for name in ("maxpm", "maxpm-manager"):
        src, dst = GUIDES / name, dest / name
        if not (src / "SKILL.md").is_file():
            raise RiverError(f"no guide at {src}; reinstall MaximizePM")
        if dst.is_symlink():
            dst.unlink()
        elif dst.exists():
            if not force:
                raise RiverError(f"{dst} is a folder, not a link; it may hold your own edits. "
                                 f"Replace it with: maxpm skills install --force")
            shutil.rmtree(dst)
        if copy:
            shutil.copytree(src, dst)
            out.append(f"copied {dst}")
        else:
            dst.symlink_to(src.resolve(), target_is_directory=True)
            out.append(f"linked {dst} -> {src.resolve()}")
    return out


def activity_command(stdin=None, agent_id="claude"):
    """maxpm report --hook <agent> (and its old name maxpm activity, for claude): an agent's hook passes its event on
    stdin (Claude Code: JSON; the adapter's read_hook reads it, core.hook_report records it). It prints nothing (but
    the Stop hook's reminder to close answered questions, #5001), waits at most two seconds for the queue, and
    always exits 0: a failed record never stops the session (#4288)."""
    import json
    conn = None
    try:
        payload = json.loads((stdin or sys.stdin).read() or "{}")
        conn = core.connect()
        conn.execute("PRAGMA busy_timeout=2000")
        res = core.hook_report(conn, agent_id, payload if isinstance(payload, dict) else {})
        if res["prompt"] or res["state"] == "idle":
            _record_hook_process(conn, res["agent"])
        if res["state"] == "idle" and core.pending_waits(conn, res["agent"]):
            # A message that the person sent during this turn waits (#5587): maxpm serve types it at the idle prompt.
            try:
                ask_server(conn, "pending_deliver", {"agent": res["agent"]}, timeout=1, retries=0)
            except Exception:  # noqa: BLE001 - its next pass types it
                pass
        if res.get("deny"):
            # A push or a launch while an alert is unread (#5381): Claude Code refuses the call and shows the reason.
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": res["deny"]}}))
        if res.get("remind"):
            # The Stop hook keeps the session for one more turn, to close its answered questions (#5001); Claude
            # Code sets stop_hook_active on the next Stop, so it reminds once.
            print(json.dumps({"decision": "block", "reason": res["remind"]}))
    except Exception:  # noqa: BLE001 - a hook must never fail the session
        pass
    finally:
        if conn is not None:
            conn.close()
    return 0


def wake_command(stdin=None, env=None):
    """maxpm report --wake claude: the listener hook of a Claude Code session (#5686; core.wake_listen). Claude Code
    runs it in the background (asyncRewake) and passes the hook's JSON on stdin, with the session id. It waits for a
    message to the session; then it prints the text on stderr and exits 2, and Claude Code shows the text to the
    session at once. It exits 0 with no output when a newer listener took over or the session ended, and on any
    error: a hook must never fail the session."""
    import json
    env = os.environ if env is None else env
    try:
        payload = json.loads((stdin or sys.stdin).read() or "{}")
        sid = (payload.get("session_id") if isinstance(payload, dict) else None) or env.get("CLAUDE_CODE_SESSION_ID")
        if not sid:
            return 0
        parent = int(env["CLAUDE_PID"]) if str(env.get("CLAUDE_PID") or "").isdigit() else os.getppid()
        text = core.wake_listen(sid, parent=parent)
    except Exception:  # noqa: BLE001 - a hook must never fail the session
        return 0
    if not text:
        return 0
    sys.stderr.write(text.rstrip("\n") + "\n")
    return 2


# The commands that name the agent of the session they run in: a session may take a new name with them (its agent
# ended, or MaximizePM names it), and its session id, link and process then go to that name.
NAMING = ("go", "register", "session", "manage")


def _session_self(conn):
    """The agent this command's session is (#5576): MAXPM_AGENT while it names a registered AI agent; else the agent
    that MaximizePM recorded last for this session id (it may be gone now); else MAXPM_AGENT when it names no agent
    yet (a session that maxpm serve started and named, before its first go); else None (a person's terminal). In
    a chat over MCP HTTP the environment is maxpm serve's, not the chat's: None."""
    if core.HTTP_CHAT.get():
        return None
    me = (os.environ.get("MAXPM_AGENT") or "").strip()
    row = me and conn.execute("SELECT kind FROM agents WHERE name=?", (me,)).fetchone()
    if row and row["kind"] == "ai":
        return me
    return core.session_owner(conn, core.session_from_env()) or (me if me and not row else None)


def _for_other(conn, actor, cmd=None):
    """Whether this command runs, in the session of one AI agent, as another AI agent (--as). Only the manager may
    (mark 2026-11-10, #5060: 'the manager should be allowed to run for other agents'); its command then records
    nothing of its session for that agent: no session id, no link, no process (with them, every hook of the
    manager's session counted as the other agent, and tmux_adopt took the manager for a session outside tmux).
    Another session is refused (mark 2026-11-12, #5576: 'this should not be allowed or possible'). A person's
    terminal may act as anyone.

    The session is its MAXPM_AGENT, or the agent recorded last for its session id (_session_self). Two sessions
    had no MAXPM_AGENT that named a registered agent, and each ran 'maxpm --as <manager> who' after its agent
    unregistered: #5088 (maximizepm-873b, 2026-11-10) and #5576 (maximizepm-1d68, 2026-11-12). Each recorded the
    worker's session as the manager's newest, so the manager's chat on the Sessions tab showed the worker's ended
    chat. Such a session is refused too; only go and register may give it a new name."""
    own = _session_self(conn)
    if not (actor and own and actor != own):
        return False
    if not conn.execute("SELECT 1 FROM agents WHERE name=? AND kind='ai'", (actor,)).fetchone():
        return False  # a person, or a name that go or register gives this session now
    caller = conn.execute("SELECT role FROM agents WHERE name=? AND kind='ai'", (own,)).fetchone()
    if caller and caller["role"] == "manager":
        return True
    if not caller and cmd in ("go", "register"):
        return False  # the agent of this session is gone: the session takes another name
    manager = core.active_manager(conn)
    raise RiverError(f"refused: this session is {own}, and only the manager runs a command as another agent "
                     f"(--as {actor}). Run it as yourself, or ask the manager: maxpm --as {own} alert "
                     f"{manager or '<manager>'} \"<what {actor} needs>\"")


def _carries_no_name(conn, sid):
    """Whether session id `sid` is a new one of an agent CLI that gives MAXPM_AGENT to no command (Hermes, Aider:
    their commands name the agent with --as only): no MAXPM_AGENT, no agent has the id yet, and Claude Code did not
    give it. Any command of such a session records its id (#5212). A Claude Code session has MAXPM_AGENT when maxpm
    serve started it and records its id with go when a person did, so a Claude Code id with neither is a person's
    own chat, which is no agent's (#5576)."""
    return bool(sid and not (os.environ.get("MAXPM_AGENT") or "").strip() and not core.session_owner(conn, sid)
                and sid != core.claude_session_from_env())


def _record_self(conn, name, cmd, for_other):
    """Record this command's session id and session link for agent `name`, only when the session is that agent's
    own (#5576): never for a command as another agent (for_other), never in a chat over MCP HTTP, and for any
    command but a naming one (NAMING) only when the session is `name` (_session_self). A session id that another
    registered agent has is not taken (core.record_session); only a naming command, or maxpm session --move run
    by a person or the manager, moves it."""
    if for_other or not name or core.HTTP_CHAT.get():
        return
    naming = cmd in NAMING
    sid = core.session_from_env()
    if not naming and name != _session_self(conn) and not _carries_no_name(conn, sid):
        return
    if core.record_session(conn, name, sid, move=naming) is not False:
        core.record_session_url(conn, name, core.session_url_from_env())


def _record_hook_process(conn, name):
    """A hook runs outside the agent's sandbox, where ps works: record the agent CLI process when the agent's own
    commands could not (a sandbox blocks ps, so agents.pid stays empty, #4293). maxpm serve then sees the commands
    that run below the session (busy_now) and keeps its leases. Only at a prompt or a turn end, and only when no
    live process of this host is recorded: the tool calls stay fast."""
    terminal = bool(name) and core.process_on_terminal(conn, name)
    if terminal:
        core.drop_background_process(conn, name)
    r = name and conn.execute("SELECT pid, host FROM agents WHERE name=? AND kind='ai'", (name,)).fetchone()
    if not r or (r["pid"] and r["host"] == core.this_host() and core.pid_alive(r["pid"])):
        return
    pid, cmd = core.find_agent_process(conn, name)
    if pid:
        core.set_process(conn, name, pid, cmd)


def run(argv=None):
    words = sys.argv[1:] if argv is None else list(argv)
    if words[:1] == ["activity"]:
        return activity_command()  # first: a hook runs on every tool call, so it skips the parser and the briefing
    if words[:2] == ["report", "--hook"] and len(words) == 3:
        return activity_command(agent_id=words[2])
    if words[:2] == ["report", "--wake"] and len(words) == 3:
        return wake_command()
    args = parse_args(argv)
    if args.cmd is None:
        print(QUICKSTART)
        return 0
    if args.cmd == "guide":
        if args.which == "setup":
            print(SETUP)
        elif args.which == "decisions":
            print(core.DECISION_FORMAT)
        else:
            name = {"manager": "maxpm-manager"}.get(args.which, "maxpm")
            text = (GUIDES / name / "SKILL.md").read_text(encoding="utf-8")
            print(text.split("---", 2)[2].strip() if text.startswith("---") else text)
        return 0
    if args.cmd == "init":
        return init_folder(args)
    if args.cmd == "skills":
        for line in install_skills(Path(args.dir).expanduser(), args.copy, args.force):
            print(line)
        return 0
    if args.cmd == "db":
        print(core.db_path())
        return 0
    if args.cmd == "mcp":
        from . import mcp
        mcp.serve()
        return 0
    if args.cmd == "connect":
        return connect_command(args)
    if args.cmd == "ops" and args.ocmd == "logs":  # a plugin: its own lines only, no briefing, no footer
        from .plugins import logs
        res = logs.run(args.name, args.file, args.ssh, args.command, args.pattern, args.ignore, args.warning,
                       args.critical, args.per_minute, args.from_start, args.timeout)
        print(json.dumps(res) if args.json else "\n".join(logs.lines(res)))
        return res["status"]
    if args.cmd == "ops" and args.ocmd == "pages":  # a plugin (#5516): its own lines only
        from .plugins import pages
        if args.pgcmd == "maps":
            conn = core.connect()
            try:
                res = pages.build_maps(conn, args.project, args.rev)
            finally:
                conn.close()
            if args.json:
                print(json.dumps(res))
            elif res.get("error"):
                raise RiverError(res["error"])
            else:
                print(f"source maps of {res['rev'][:12]}: {sum(len(v) for v in res['files'].values())} map(s) for "
                      f"{len(res['files'])} script name(s), in {pages.map_dir(args.project, res['rev'])}"
                      + (f" ({res['note']})" if res.get("note") else ""))
            return 0
        res = pages.run(args.name, args.project, args.pages, args.settle, args.timeout, args.rev)
        print(json.dumps(res) if args.json else "\n".join(pages.lines(res)))
        return res["status"]
    if args.cmd == "ops" and args.ocmd == "visual" and args.vcmd in ("run", "show"):  # a plugin: its own lines only
        from .plugins import visual
        if args.vcmd == "show":
            res = visual.show(args.name)
            print(json.dumps(res) if args.json else "\n".join(visual_show_lines(res)))
            return 0
        res = visual.run(args.name, args.url, args.widths, args.themes, args.threshold, args.wait, args.full,
                         args.mask, args.timeout)
        print(json.dumps(res) if args.json else "\n".join(visual.lines(res)))
        return res["status"]
    if args.cmd == "ops" and args.ocmd == "check":  # the watchdog (#5441): no activity, no footer; its log is the output
        from . import login, notify
        conn = core.connect()
        try:
            core.adopt_login_path(conn, max_age=24 * 3600)  # launchd's short PATH: the checks need a terminal's (#5611)
            res = core.ops_outside(conn, lambda title, body: notify.direct(conn, title, body),
                                   serve_wanted=login.serve_wanted())
        finally:
            conn.close()
        print(json.dumps(res) if args.json else "\n".join(ops_outside_lines(res)), flush=True)
        return 0
    if args.cmd == "ops" and args.ocmd == "probe":  # a plugin: its own lines only, no briefing, no footer, no writes
        conn = core.connect()
        try:
            res = core.ops_probe(conn, args.name)
        finally:
            conn.close()
        print(json.dumps(res) if args.json else "\n".join(core.ops_probe_lines(res)))
        return core.OPS_PROBE_EXIT[res["status"]]
    if args.cmd == "setup-agent":
        if args.claude_desktop and args.codex:
            raise RiverError("one app at a time: --claude-desktop or --chatgpt-desktop")
        if args.claude_desktop:
            from . import agents
            print(agents.get("claude").write_mcp_config(args.config, args.remove))
            return 0
        if args.codex:
            from . import agents
            print(agents.get("codex").write_mcp_config(args.config, args.remove))
            return 0
        if args.remove or args.config:
            raise RiverError("--remove and --config go with --claude-desktop or --chatgpt-desktop")
        if not args.append:
            print(AGENT_SNIPPET)
            return 0
        f = Path(args.append)
        old = f.read_text(encoding="utf-8") if f.exists() else ""
        if core.names_product(old):
            print(f"{f} already mentions {core.PRODUCT}; nothing added")
            return 0
        f.write_text(old + ("\n" if old and not old.endswith("\n") else "") + ("\n" if old else "") + AGENT_SNIPPET, encoding="utf-8")
        print(f"added the work queue block to {f}")
        return 0
    conn = core.connect()
    # One command reads the item graph once while it writes nothing (#5491). serve keeps its own (page_state).
    # In a runner of maxpm serve the share lives as long as the runner: its keys check the connection's writes,
    # other connections' writes and the clock, so a result serves the next command while nothing changed (#5501).
    share = (core.ANN_SHARE.set(core.RESIDENT.setdefault("share", {}) if core.RESIDENT else {})
             if args.cmd not in LONG_COMMANDS else None)
    try:
        return _run(args, conn)
    finally:
        if share is not None:
            core.ANN_SHARE.reset(share)
        conn.close()


LONG_COMMANDS = ("serve",)
# The commands whose answer reads the needs-you rows: activity brings them up to date first (maxpm needs-you syncs
# them itself). Every other command syncs them only when the sweep or the command itself changed the queue (#5491);
# maxpm serve syncs them every notify_interval.
READS_NEEDS_YOU = ("cleanup", "health")


def _run(args, conn):
    if args.cmd == "serve" and args.restart:
        res = serve_restart(conn)
        if res.get("waits"):
            print(f"maxpm serve starts again when this ends (a restart now would cut it): {res['waits']}")
            return 0
        print(f"maxpm serve started again and answers on port {core.setting(conn, 'serve_port')}"
              + ("" if not res["stale"] else "; the code changed again since: run it once more")
              if res["restarted"] else
              "maxpm serve took the request but did not answer again within 60s: look at its terminal")
        return 0 if res["restarted"] else 1
    if args.cmd == "serve" and (args.install_watchdog or args.remove_watchdog):
        from . import login
        if args.remove_watchdog:
            res = login.remove_watchdog()
            print(f"Removed {res['file']}: the watchdog no longer runs." if res["removed"]
                  else "The watchdog was not installed.")
            return 0
        res = login.install_watchdog(conn)
        n = res["checks"]
        print(f"The watchdog runs maxpm ops check --outside every {res['every']} s: {res['file']}. Its output goes to "
              f"{res['log']}. " + (f"It runs {n} check{'' if n == 1 else 's'} marked outside now." if n else
                                   "No check is marked outside yet: maxpm ops edit <scope> <name> --outside on")
              + " Undo: maxpm serve --remove-watchdog")
        return 0
    if args.cmd == "serve" and args.kick:
        from . import login
        res = login.kick()
        print("maxpm serve " + ("started through its login agent" if res["how"] == "loaded" else
                                "stopped; the desktop app starts it again" if res["how"] == "app" else
                                "stopped and started again through its login agent"))
        return 0
    if args.cmd == "serve" and (args.install_login or args.remove_login):
        from . import login
        if args.remove_login:
            res = login.remove()
            print(f"Removed {res['file']}: maxpm serve no longer starts at login." if res["removed"]
                  else "maxpm serve was not set to start at login.")
            return 0
        res = login.install(conn)
        how = ("it runs now" if res["started_now"] else
               f"{res['system']} starts it at the next login (an agent is loaded already)" if res["agent_loaded"] else
               f"a maxpm serve already answers on port {res['port']}, so {res['system']} starts it at the next login")
        print(f"maxpm serve starts at login and again after a crash: {res['file']} ({how}). Its output goes to "
              f"{res['log']}. Undo: maxpm serve --remove-login"
              + (". To start it at boot before anyone logs in: loginctl enable-linger" if res["system"] == "systemd"
                 else ""))
        return 0
    if args.cmd == "serve":
        from . import server
        port = args.port or int(core.setting(conn, "serve_port"))
        server.serve(port, open_browser=args.open, dev=args.dev)
        return 0
    actor = args.actor
    for_other = _for_other(conn, actor, args.cmd)
    if args.cmd not in ("go", "plan"):
        core.activity(conn, actor, sync=args.cmd in READS_NEEDS_YOU)
    st = core.stop_request(conn, actor) if args.cmd not in ("go", "wait", "stop") else None
    if st and not args.json:
        print(_stop_banner(actor, st), file=sys.stderr)
    _record_self(conn, actor, args.cmd, for_other)  # before done measures the item
    changes = conn.total_changes
    try:
        res = dispatch(conn, args, actor)
    except RiverError:
        # An event that occurred before the refusal still runs its hook.
        for line in _hook_result_lines(run_hooks(conn, actor)):
            print(line, file=sys.stderr)
        raise
    # The events of this command (a release cut, a review, a deploy): their hooks run now.
    hooks = run_hooks(conn, actor)
    if hooks and isinstance(res, dict):
        res["hooks_run"] = hooks
    me = (res.get("agent") if args.cmd in ("go", "plan", "manage") and isinstance(res, dict) else
          args.name if args.cmd == "register" and not args.human else actor)
    _record_self(conn, me, args.cmd, for_other)
    if conn.total_changes != changes:  # the command may have made a human item ready, or sent a question to a person
        core.sync_needs_you_now(conn)
    if (args.cmd == "view" and args.layout and res.get("show") and res["panes"] and not args.json
            and sys.stdin.isatty() and sys.stdout.isatty()):
        conn.close()
        # tmux takes the screen next: these lines show again when the person leaves the view.
        for p in res["closed"]:
            print(f"closed ({p['why']}): {p['name']}")
        done = sum(bool(p["done"]) for p in res["panes"])
        if done:
            print(f"{done} of the {len(res['panes'])} panes: the session is done. maxpm view --tidy closes "
                  f"{'it' if done == 1 else 'them'}; maxpm view --list says which and why.")
        sys.stdout.flush()
        os.execvp(res["show"][0], res["show"])  # tmux takes this terminal: the agents, side by side
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        render(args, res)
        for line in _hook_result_lines(hooks):
            print(line)
    sys.stdout.flush()
    if not (args.cmd in ("wait", "unregister") and conn.execute("SELECT 1 FROM agents WHERE name=?", (actor,)).fetchone() is None):
        _footer(conn, res["agent"] if args.cmd == "go" else actor)
    if not args.quiet and not args.json:
        h = _hint(args, res, actor)
        if h:
            print(h, file=sys.stderr)
    return 0


def _record_process(conn, name):
    """The agent CLI process that runs this command (for maxpm who and maxpm stop --kill). An agent whose adapter
    runs its tools in a shared background process (Hermes) records only a process on a terminal (#5247)."""
    if not name or not conn.execute("SELECT 1 FROM agents WHERE name=? AND kind='ai'", (name,)).fetchone():
        return
    terminal = core.process_on_terminal(conn, name)
    pid, cmd = core.find_agent_process(conn, name)
    if pid:
        core.set_process(conn, name, pid, cmd)
    elif terminal:
        core.drop_background_process(conn, name)


def run_hooks(conn, actor=None):
    """The hooks of the events of this command run now (core.run_hooks). In the sandbox of an agent session the
    hook would have the limits of that sandbox, so maxpm serve runs it, outside every sandbox, and this command
    waits for the results and prints them (#1826). With no serve that answers, it runs here and says so."""
    if not in_sandbox() or not core.hooks_waiting(conn):
        return core.run_hooks(conn, os.getcwd())
    token = core.hook_token()
    try:
        ask_server(conn, "run_hooks", {"token": token, "cwd": os.getcwd()}, actor=actor, retries=1)
    except RiverError as e:
        out = core.run_hooks(conn, os.getcwd(), sandbox=True)
        for h in out:
            h["serve_error"] = str(e)
        return out
    out = core.hook_results(conn, token, os.getcwd())
    for h in out:
        h["served"] = not h.get("sandbox")
    return out


def in_sandbox():
    """This command runs in an agent CLI's sandbox (Claude Code sets SANDBOX_RUNTIME, Codex CODEX_SANDBOX):
    it cannot script Terminal or reach the tmux socket, so it cannot open a session itself."""
    return bool(os.environ.get("SANDBOX_RUNTIME") or os.environ.get("CODEX_SANDBOX"))


RETRY_WAIT = 1.0  # seconds between two tries to reach a maxpm serve that does not listen



def _browser_cmd(conn, a, actor):
    """maxpm browser: maxpm serve runs the browser (river/browser.py); the command asks it (ask_server)."""
    b = a.bcmd
    if b == "fallbacks":  # #5692: the queue's own table, no need of maxpm serve
        return core.browser_fallbacks(conn, a.site, a.since, a.n)
    if b in ("annotations", "annotation"):  # the queue's own table (#5655): no need of maxpm serve
        from . import annotate
        if b == "annotation":
            if a.change:
                if a.n is None:
                    raise RiverError(f"name the note: maxpm browser annotation {a.id} {a.change} <n> --note \"...\"")
                return annotate.mark(conn, a.id, a.n, a.change, a.note, actor)
            return annotate.show(conn, a.id)
        held = conn.execute("SELECT id FROM items WHERE assignee=? AND status IN ('in_progress','held')",
                            (actor,)).fetchall() if actor else []
        project = a.project
        if a.all or not actor:
            from . import browser
            project = project or browser.project_of(conn, None, actor, os.getcwd())
            return {"sets": annotate.sets(conn, project=project, open_only=a.open)}
        out = {}
        for i in [None] + [r["id"] for r in held]:
            for s in annotate.sets(conn, agent=actor, item_id=i, project=project, open_only=a.open):
                out[s["id"]] = s
        return {"sets": sorted(out.values(), key=lambda s: -s["id"])}
    if getattr(a, "json", False):  # #5678: the one reply shape (browser.shape), as op browser_call and the MCP tool
        from .browser import shape
        action = "batch" if b == "batch" else None
        try:
            res = _browser_ask(conn, a, actor)
            action = action or res.pop("_action")
            out = shape(action, res)
        except RiverError as e:
            out = shape(action or _browser_args(a)["action"], error=e)
        if not out["ok"]:
            print(json.dumps(out))
            raise SystemExit(2)
        return out
    return _browser_ask(conn, a, actor)


def _browser_ask(conn, a, actor):
    """One maxpm browser command to serve (op browser), and what the command does with the reply: a screenshot's file,
    the HTML's file or size."""
    b = a.bcmd
    if b == "batch":
        steps = _browser_batch(a)
        return ask_server(conn, "browser", {"action": "batch", "steps": steps, "project": a.project, "tab": a.tab,
                                            "cwd": os.getcwd()}, actor, timeout=120 + 30 * len(steps))
    if b == "run":  # #5680: the lines of a walk with their inputs, as one call
        inputs = {}
        for x in a.inputs:
            k, eq, v = x.partition("=")
            if not eq or not k:
                raise RiverError(f"an input is name=value, not {x!r}: maxpm browser run q=rust < walk.txt")
            inputs[k] = v
        steps = _browser_batch(a, inputs)
        return ask_server(conn, "browser", {"action": "run", "steps": steps, "project": a.project, "tab": a.tab,
                                            "cwd": os.getcwd()}, actor, timeout=120 + 30 * len(steps))
    args = {**_browser_args(a), "project": a.project, "cwd": os.getcwd()}
    if a.tab:
        args["tab"] = a.tab
    res = ask_server(conn, "browser", args, actor, timeout=120)
    if getattr(a, "json", False):
        res["_action"] = args["action"]
    if b == "screenshot":
        import base64
        import tempfile
        out = Path(a.out).expanduser() if a.out else Path(tempfile.gettempdir()) / (
            f"maxpm-browser-{res['project']}-{time.strftime('%Y%m%d-%H%M%S')}.png")
        out.write_bytes(base64.b64decode(res.pop("png")))
        res["file"] = str(out.resolve())
        e = res.get("element")
        if e:  # #5689: the scale, so a point in the picture becomes --at
            res["file"] += (f"  ({e['picture'][0]}x{e['picture'][1]} pixels of a box of {e['box'][0]}x{e['box'][1]}, "
                            f"scale {e['scale']:g}" + ("; the grid lines are each 10%" if e["grid"] else "") +
                            f"; a point: maxpm browser click {e['target']} --at <x%>,<y%>)")
        if a.cursor and not res.get("cursor"):
            print("maxpm: no session pointed in this tab yet: the picture has no pointer", file=sys.stderr)
    if b == "html" and a.out:
        Path(a.out).expanduser().write_text(res.pop("html"), encoding="utf-8")
        res["file"] = str(Path(a.out).expanduser().resolve()) + f" ({res.get('bytes', 0)} bytes)"
    elif b == "html" and not getattr(a, "json", False):
        print(f"maxpm: {res.get('bytes', 0)} bytes of HTML", file=sys.stderr)  # #5671: the size, apart from the HTML
    return res


def _is_number(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _browser_args(a):
    """The arguments of one maxpm browser action for serve (routes/browser.act), from its parsed words."""
    b = a.bcmd
    args = {"action": {"new-tab": "new_tab", "close-tab": "close_tab", "headless": "window",
                       "record-steps": "record_steps"}.get(b, b)}
    if getattr(a, "wait", None):
        args["wait"] = a.wait
    if getattr(a, "snapshot", False):
        args["snapshot"] = True
    if getattr(a, "within", None):
        args["within"] = a.within
    if getattr(a, "mods", None):  # #5669: --with shift,ctrl
        args["mods"] = [m.strip().lower() for m in a.mods.split(",") if m.strip()]
    for k in ("at", "to_at"):  # #5689: a point inside the target's box
        if getattr(a, k, None):
            args[k] = a.at if k == "at" else a.to_at
    if b == "open":
        args.update(url=a.url, show=not a.no_show, outline=not a.no_outline, read=a.text)
    elif b == "extract":  # #5677: the fields are checked here, before serve gets them
        from .browser import parse_field
        args.update(rows=a.rows, fields=[parse_field(f) for f in a.fields], limit=a.limit)
    elif b == "text":  # #5672
        args.update({k: v for k, v in (("section", a.section), ("grep", a.grep), ("from", a.start), ("max", a.max),
                                       ("all", a.all)) if v})
    elif b == "snapshot":
        args.update(interactive=a.interactive, scope=a.scope, delta=a.delta, max=a.max, all=a.all, region=a.region)
    elif b == "find":  # #5671: a snapshot of the matching elements
        args.update(action="snapshot", interactive=True, find=" ".join(a.words))
    elif b == "html" and a.scope:
        args["scope"] = a.scope
    elif b == "screenshot":
        args.update(full=a.full, cursor=a.cursor, grid=a.grid, selector=a.target)
        if a.target and a.full:
            raise RiverError("a picture of one element is not of the whole page: leave out --full")
    elif b == "click":
        t, css = a.target, "css:" + a.selector if a.selector else None
        if t is not None and _is_number(t):
            if a.y is None or not _is_number(a.y):
                raise RiverError("a point needs x and y: maxpm browser click <x> <y>")
            args.update(x=float(t), y=float(a.y), selector=css)
        elif t is not None or css:
            if t is not None and a.y is not None:
                if t not in BROWSER_ROLES:
                    raise RiverError(f"click takes one element or a point, not {t!r} and {a.y!r}: put a name in "
                                     f"quotes, or name its role first (link \"{a.y}\")")
                t = f'{t} "{a.y}"'
            args["selector"] = t if t is not None else css
        else:
            raise RiverError("name what to click: maxpm browser click \"<its name>\", maxpm browser click @e3 (a ref "
                             "of maxpm browser snapshot), --selector '<css>', or maxpm browser click <x> <y>")
        if a.double:
            args["count"] = 2
        if a.right:
            args["button"] = "right"
    elif b == "hover":  # #5669: an element or a point, as click takes them
        t = a.target
        if t is not None and _is_number(t):
            if a.y is None or not _is_number(a.y):
                raise RiverError("a point needs x and y: maxpm browser hover <x> <y>")
            args.update(x=float(t), y=float(a.y))
        elif t is not None:
            args["selector"] = f'{t} "{a.y}"' if a.y is not None and t in BROWSER_ROLES else t
        else:
            raise RiverError("name what to hover: maxpm browser hover \"<its name>\", @e3, or <x> <y>")
    elif b == "scroll":  # [target] [amount]: a last word that is a number or a direction is the amount
        w = list(a.words)
        if w and (_is_number(w[-1]) or w[-1].lower() in ("down", "up", "left", "right")):
            args["amount"] = w.pop()
        if len(w) > 1:
            raise RiverError("scroll takes one element (in quotes) and an amount: maxpm browser scroll \"Results\" down")
        if w:
            args["selector"] = w[0]
    elif b == "drag":
        def point(s):
            p = s.split(",")
            return [float(p[0]), float(p[1])] if len(p) == 2 and all(_is_number(v) for v in p) else None
        src, dst = point(a.source), point(a.dest)
        if src:
            args.update(x=src[0], y=src[1])
        else:
            args["selector"] = a.source
        args["to"] = dst or a.dest
    elif b == "type":
        if len(a.words) > 2:
            raise RiverError("type takes the text in one argument, in quotes: maxpm browser type @e5 'two words'")
        if len(a.words) == 2 and a.selector:
            raise RiverError("name the field once: maxpm browser type @e5 'text', or --selector")
        args.update(text=a.words[-1], selector=a.words[0] if len(a.words) == 2 else
                    "css:" + a.selector if a.selector else None)
    elif b == "fill":
        args.update(selector=a.target, text=a.text)
    elif b == "select":
        args.update(selector=a.target, values=a.values)
    elif b in ("check", "uncheck"):
        args["selector"] = a.target
    elif b == "key":
        args["key"] = a.key
    elif b == "size":  # #5676
        args.update(size=a.size, dpr=a.dpr)
    elif b in ("steps", "errors"):  # #5680, #5675
        args["clear"] = a.clear
    elif b == "record":  # #5682
        if a.do == "step" and a.n is None:
            raise RiverError("name the step: maxpm browser record step <n> (maxpm browser record lists them)")
        args.update(record=a.do, name=a.name, n=a.n)
    elif b == "record-steps":  # #5707
        args["do"] = a.do
    elif b == "upload":  # #5679: full paths, as serve runs in another folder
        args.update(selector=a.target, files=[str(Path(f).expanduser().resolve()) for f in a.files])
    elif b == "image":  # #5688: serve writes the file, so a full path
        args.update(selector=a.target, out=str(Path(a.out).expanduser().resolve()) if a.out else None)
    elif b == "dialog":
        if a.text is not None and a.answer == "dismiss":
            raise RiverError("dismiss takes no text: maxpm browser dialog dismiss")
        args.update(accept=a.answer == "accept", text=a.text)
    elif b == "eval":
        args["js"] = a.js
    elif b == "give":
        args["sub"] = a.label
    elif b == "control":
        args["take"] = a.take
    elif b in ("tab", "close-tab"):
        args["target"] = a.id
    elif b == "new-tab":
        args["url"] = a.url
    elif b in ("window", "headless"):
        args["window"] = b == "window"
    return args


def _browser_batch(a, inputs=None):
    """The steps of maxpm browser batch and run: each line of the input, as after 'maxpm browser' (# starts a note).
    inputs (run, #5680): each {name} in a word takes the input's value."""
    import re
    import shlex
    text = a.lines if getattr(a, "lines", None) is not None else \
        Path(a.file).expanduser().read_text(encoding="utf-8") if a.file else sys.stdin.read()
    parser, steps = build_parser(only="browser"), []
    what = "run" if inputs is not None else "batch"
    for n, line in enumerate(text.splitlines(), 1):
        try:
            words = shlex.split(line, comments=True)
        except ValueError as e:
            raise RiverError(f"line {n} of the {what}: {e}")
        if not words:
            continue
        if words[0] == "maxpm" and words[1:2] == ["browser"]:
            words = words[2:]
        elif words[0] == "browser":
            words = words[1:]
        if inputs is not None:
            def put(m, n=n):
                if m.group(1) not in inputs:
                    raise RiverError(f"line {n} of the run needs the input {m.group(1)}: maxpm browser run "
                                     f"{m.group(1)}=<value>")
                return inputs[m.group(1)]
            words = [re.sub(r"\{([A-Za-z_]\w*)\}", put, w) for w in words]
        if words and words[0] in ("batch", "run", "steps", "screenshot", "html", "annotations", "annotation"):
            raise RiverError(f"line {n} of the {what}: {words[0]} is not for a {what}; run it as its own command")
        try:
            sub = parser.parse_args(["browser"] + words)
        except _Retry as e:  # the parser of one command says its mistake this way, with no exit
            raise RiverError(f"line {n} of the {what} ({line.strip()}): {e}")
        step = _browser_args(sub)
        if sub.project:
            step["project"] = sub.project
        if sub.tab:
            step["tab"] = sub.tab
        steps.append(step)
    if not steps:
        raise RiverError(f"the {what} is empty: one action on each line, such as: click \"Comments\"")
    return steps


def _record_step_line(s):
    """#5682: one step of a record in one line: its number, ok or the error code, its line, its time and errors."""
    errs = len(s.get("errors") or [])
    return (f"{s['n']} {'ok' if s['ok'] else 'FAILED ' + (s.get('code') or 'error')}  {s['line']}  ({s['ms']} ms"
            + (f", {errs} page error{'s' if errs != 1 else ''}" if errs else "") + ")")


def _record_lines(res):
    """#5682: maxpm browser record start | stop | status | step <n>."""
    r, step = res.get("record"), res.get("step")
    if step:
        print(_record_step_line(step))
        for k, label in (("error", "error"), ("dialog", "box of the page"), ("title", "page"), ("url", "address")):
            if step.get(k):
                print(f"  {label}: {step[k]}")
        for x in (step.get("notes") or []) + (step.get("changes") or []) + (step.get("errors") or []):
            print(f"  {x}")
        print(f"  picture: {step['picture']}" if step.get("picture") else "  (no picture: the page was not there)")
        return
    if not r:
        print("no record in this browser: maxpm browser record start")
        return
    failed = r.get("failed") or []
    head = f"record {r['name']}: {r['steps']} steps" + (f", failed at step {', '.join(map(str, failed))}" if failed else "")
    if r.get("over"):
        head += f", {r['over']} actions after the last step not recorded"
    if res.get("on") and "steps" not in res:  # start
        print(f"record {r['name']} is on: each action adds a step with its picture. "
              "maxpm browser record stop puts the steps as a canvas")
        return
    if "canvas" in res:  # stop
        c = res["canvas"]
        print(head + (f"; canvas {c['name']} v{c['version']} ({c['bytes'] // 1000} KB), in your side panel"
                      if c else "; no steps, so no canvas"))
        if failed:
            print(f"maxpm browser record step {failed[0]}: the step that failed and the path of its picture")
        return
    print(head + ("" if res.get("on") else " (stopped)"))
    steps = res.get("steps") or []
    for s in steps[-10:]:
        print("  " + _record_step_line(s))
    if len(steps) > 10:
        print(f"  ({len(steps) - 10} earlier steps: maxpm browser record step <n>)")

def ask_server(conn, op, args, actor=None, timeout=5, retries=3):
    """Ask the maxpm serve of this queue to do one page action (server.OPS) and return its result; RiverError
    says why not. The server runs outside any sandbox. A sandbox refuses a direct connection to this computer;
    its HTTP proxy passes the request when the sandbox allows the host, so the sandbox still decides."""
    import base64
    import urllib.error
    import urllib.request
    from urllib.parse import unquote, urlsplit
    port = core.serve_port_now(conn)  # the app's serve may run on a port of its own (#5618)
    host = f"127.0.0.1:{port}"
    body = json.dumps({"op": op, "actor": actor, "args": {**args, "db": str(core.db_path())}}).encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # this computer: no proxy by itself

    def post(proxy=None):
        req = urllib.request.Request(f"http://{host}/api/action", data=body, headers={"Content-Type": "application/json"})
        if proxy:
            if proxy.username:
                cred = f"{unquote(proxy.username)}:{unquote(proxy.password or '')}".encode()
                req.add_header("Proxy-Authorization", "Basic " + base64.b64encode(cred).decode())
            req.set_proxy(f"{proxy.hostname}:{proxy.port or 80}", "http")
        try:
            with opener.open(req, timeout=timeout) as r:
                return json.loads(r.read())["result"]
        except urllib.error.HTTPError as e:
            with e:
                text = e.read().decode(errors="replace").strip()
            said = {}
            try:
                said = json.loads(text)
                why = said["error"]  # maxpm serve refused: its own words
            except (ValueError, KeyError, TypeError):
                why = None
            if why and isinstance(said, dict) and said.get("code"):  # a browser refusal's code (#5678)
                err = RiverError(why)
                err.code, err.candidates = said["code"], said.get("candidates") or []
                raise err
            if why is None and proxy and (e.code == 403 or e.headers.get("X-Proxy-Error")):
                raise RiverError(f"the sandbox around this session refused the connection to maxpm serve on {host}"
                                 f" ({text.splitlines()[0] if text else e.reason}). Allow the host {host} for this "
                                 f"command and run it again (Claude Code: the command's allowed_domains), or run it "
                                 f"outside the sandbox")
            raise RiverError(why or f"no maxpm serve answers on port {port} (HTTP {e.code}): start it with `maxpm serve`")
    def ask():
        try:
            return post()
        except OSError as e:
            if not isinstance(getattr(e, "reason", e), PermissionError):
                raise
            # The sandbox refused the direct connection. Its proxy is the way out that it controls.
            proxy = urlsplit(os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "")
            if proxy.scheme != "http" or not proxy.hostname:
                raise RiverError(f"the sandbox around this session refused the connection to maxpm serve on {host}. "
                                 f"Allow the host {host} for this command, or run it outside the sandbox")
            return post(proxy)
    try:
        for left in range(retries, -1, -1):
            try:
                return ask()
            except OSError as e:
                # Nobody listens: maxpm serve may start again just now (new code); it is back in a second or two.
                if not left or not isinstance(getattr(e, "reason", e), ConnectionRefusedError):
                    raise
                time.sleep(RETRY_WAIT)
    except (OSError, ValueError, KeyError) as e:
        raise RiverError(f"no maxpm serve answers on port {port} ({e.__class__.__name__}): start it with `maxpm serve`")


def serve_restart(conn, wait=60, sleep=None):
    """maxpm serve --restart: ask the maxpm serve of this queue to start again with the code on disk (it
    refuses code that does not load), then wait until the new one answers. An agent runs this after a
    commit that changes river's code; with no command, maxpm serve does it by itself within a pass or two."""
    sleep = sleep or time.sleep
    ans = ask_server(conn, "serve_restart", {}, timeout=90)
    if ans.get("waits"):  # a hook runs: serve starts again when it ends (#5654); nothing to wait for here
        return {"restarted": False, "stale": True, "waits": ans["waits"]}
    old = ans["boot"]
    for _ in range(int(wait)):
        sleep(1)
        try:
            st = ask_server(conn, "serve_status", {}, retries=0)
        except RiverError:
            continue  # it starts
        if st["boot"] != old:
            return {"restarted": True, "stale": st["stale"]}
    return {"restarted": False, "stale": True}


def _stop_banner(me, st):
    return (f"STOP REQUESTED by {st['stop_by'] or 'someone'}: {st['stop_reason']}. Take no new work. Commit finished "
            f"work, release or hand back your item with a note (maxpm --as {me} release <id> --note \"...\"), "
            f"then run maxpm --as {me} go once more: it ends the session.")


def _print_queue(res):
    es = res["entries"]
    print(f"queue of {res['agent']}: " + (f"{len(es)} entr{'y' if len(es) == 1 else 'ies'}" if es else "empty"))
    for e in es:
        if e.get("item"):
            st = ("ready" if e["ready"] else e["status"].replace("_", " ") if e["status"] != "open"
                  else "waits on " + ",".join(f"#{b}" for b in e["open_blockers"]) if e["open_blockers"] else "not ready")
            print(f"  #{e['item']:<4} [{e['project']}] {e['title']}  ({st})")
        else:
            print(f"  e{e['entry']:<3} {'STOP REQUEST' if e['kind'] == 'stop' else 'instruction'}: {e['body']}"
                  + ("" if e["delivered_at"] else "  (not read yet)")
                  + (f"  [native: {e['native_status']}]" if e.get("native_status") else ""))


def _hint(a, res, actor):
    c = a.cmd
    if c in ("go", "plan"):
        return None
    if actor and c == "done":
        return ((f"what changed, for a person: maxpm --as {actor} edit {res['id']} --changed \"<one plain line>\"\n"
                 if isinstance(res, dict) and not res.get("changed") and res.get("id") else "")
                + f"next: maxpm --as {actor} go   (now: keep going while go gives you items, unless auto_continue is off; "
                f"with tree, go gives only the tasks of your own tree)")
    if c == "report" or (c == "release" and a.id is None):
        return None  # a hook runs it: no hint
    if actor and (c == "release" or c == "review"):
        return f"next: maxpm --as {actor} go"
    if actor and c == "goal" and a.gcmd == "done":
        return f"next: maxpm --as {actor} go --role owner   (takes the highest-ranked goal nobody owns)"
    if c == "register":
        return HINTS["register"].format(name=res["name"])
    if c == "next":
        if not res:
            return None  # render already says what to try
        if a.claim:
            return HINTS["claim"].format(id=res[0]["id"])
        return f"take it: maxpm claim {res[0]['id']}   (or add --claim to maxpm next)" + ("" if actor else "\n" + HINTS["no_actor"])
    if c == "claim":
        return HINTS["claim"].format(id=res["id"])
    if c == "done":
        return HINTS["done"].format(id=res["id"])
    if c == "release":
        return HINTS["release"]
    if c == "inbox" and isinstance(res, dict):
        me = f"maxpm --as {actor}"
        if res["result"] == "stop":
            return f"stop: {me} go"
        return (f"act on them, then start it again in the background: {me} inbox --wait" if res["messages"] else
                f"start it again in the background: {me} inbox --wait")
    if c == "inbox" and res:
        me = f"maxpm --as {actor}"
        return (f"answer a question: {me} answer <id> \"...\"   reply: {me} send note --reply <id> \"...\"   "
                f"whole conversation: {me} thread <id>")
    if not actor and c in ("list", "show", "who", "capacity", "blockers"):
        return HINTS["no_actor"]
    return None


def _append_block(f):
    old = f.read_text(encoding="utf-8") if f.exists() else ""
    if OLD_SNIPPET in old:  # the block of a maxpm init before #4978: its 'maxpm plan' line names a gone role
        f.write_text(old.replace(OLD_SNIPPET, AGENT_SNIPPET, 1), encoding="utf-8")
        return f"{f.name}: the work queue block is current again (the 'maxpm plan' line is gone)"
    if core.names_product(old):
        return f"{f.name}: already has the work queue block"
    f.write_text(old + ("\n" if old and not old.endswith("\n") else "") + ("\n" if old else "") + AGENT_SNIPPET, encoding="utf-8")
    return f"{f.name}: added the work queue block"


def _imports_agents(text):
    """An instruction file that imports AGENTS.md (Claude Code: a line @AGENTS.md in CLAUDE.md), so its agent reads
    the same rules as other agents. The file names and the line come from the adapters (agents.instruction_files)."""
    from . import agents
    line = agents.instruction_files()[2]
    return any(x.strip() == line for x in text.splitlines())


def _only_block(text):
    return not text.replace(OLD_SNIPPET, "").replace(AGENT_SNIPPET, "").strip()


def instructions_layout(folder):
    """How a folder's agent instructions are laid out (the names of the built-in agents; the adapters give them:
    agents.instruction_files):
    shared: CLAUDE.md imports AGENTS.md, so every agent reads one file.
    none: no rules (no file, or only the work queue block).  agents_only: the rules are in AGENTS.md.
    claude_only: the rules are in CLAUDE.md, and AGENTS.md is missing or holds only the work queue block.
    both: each file holds its own rules."""
    from . import agents
    own, shared, _, _ = agents.instruction_files()
    c, a = Path(folder, own), Path(folder, shared)
    ct = c.read_text(errors="replace", encoding="utf-8") if c.exists() else None
    at = a.read_text(errors="replace", encoding="utf-8") if a.exists() else None
    if ct is not None and _imports_agents(ct):
        return "shared"
    c_rules = ct is not None and not _only_block(ct)
    a_rules = at is not None and not _only_block(at)
    return "both" if c_rules and a_rules else "claude_only" if c_rules else "agents_only" if a_rules else "none"


def move_hint():
    """The choice that maxpm init names when only CLAUDE.md holds a folder's rules."""
    from . import agents
    own, shared, line, label = agents.instruction_files()
    readers = (agents.shared_readers() or ["Other agents"])[0]  # the first one (Codex): the line stays short
    return (f"{own} holds this folder's rules, but {readers} and other agents read {shared} and miss them. "
            f"To give every agent one file: maxpm init --move (the text of {own} goes to {shared}, and "
            f"{own} becomes the one line {line}, which {label} reads as an import)")


def setup_instructions(folder, move=None):
    """_setup_shared, and the instruction file of every other agent that imports AGENTS.md (_setup_importers), whose
    lines come before the advice that ends the lines of a folder whose rules are in CLAUDE.md (claude_only, both)."""
    layout = instructions_layout(folder)
    main, more = _setup_shared(folder, move), _setup_importers(folder)
    if layout == "both" or (layout == "claude_only" and not move):
        return main[:-1] + more + main[-1:]
    return main + more


def _setup_importers(folder):
    """The own instruction file of each agent after the first that imports AGENTS.md (Gemini CLI: GEMINI.md, #5218):
    missing, or holding only the work queue block, it becomes the one import line; holding its own rules, it gets the
    work queue block, and nothing else of it changes. Returns one line per change."""
    from . import agents
    out = []
    for own, line, label in agents.import_files()[1:]:
        f = Path(folder) / own
        text = f.read_text(errors="replace", encoding="utf-8") if f.exists() else None
        if text is not None and any(x.strip() == line for x in text.splitlines()):
            continue
        if text is None or _only_block(text):
            f.write_text(line + "\n", encoding="utf-8")
            out.append(f"{own}: {'created as' if text is None else 'now'} the one line {line}, so {label} reads "
                       f"{agents.SHARED_INSTRUCTIONS} too")
        else:
            out.append(_append_block(f))
    return out


def _setup_shared(folder, move=None):
    """Add the work queue block so that every agent reads it, and, when asked, one set of rules for all.

    AGENTS.md holds the rules and the block; CLAUDE.md imports it with @AGENTS.md (the names come from the adapters:
    agents.instruction_files). move=True moves the rules from CLAUDE.md into AGENTS.md; move=False keeps both files
    as they are (the block goes in each); move=None does what needs no choice and says the choice. Returns one line
    per change."""
    from . import agents
    own, shared, line, label = agents.instruction_files()
    folder = Path(folder)
    c, a = folder / own, folder / shared
    layout = instructions_layout(folder)
    out = []
    if layout == "claude_only" and move:
        a.write_text(c.read_text(encoding="utf-8"), encoding="utf-8")
        out += [f"{shared}: now holds the rules from {own}", _append_block(a)]
        c.write_text(line + "\n", encoding="utf-8")
        out.append(f"{own}: now the one line {line}, so {label} and every other agent read the same rules")
        return out
    if layout in ("none", "agents_only", "shared"):
        if layout != "shared" and c.exists():  # CLAUDE.md holds only the block: AGENTS.md has it now
            c.write_text(line + "\n", encoding="utf-8")
            out.append(f"{own}: now the one line {line} (it held only the work queue block)")
        out.append(_append_block(a))
        if not c.exists():
            c.write_text(line + "\n", encoding="utf-8")
            out.append(f"{own}: created as the one line {line}, so {label} reads {shared} too")
        return out
    out += [_append_block(c), _append_block(a)]
    if layout == "claude_only":
        out.append(move_hint() if move is None else f"{shared}: kept apart from {own}; it holds only the work queue block")
    else:
        out.append(f"{own} and {shared} hold different rules, and each agent reads only one of them. To share "
                   f"them: move the rules from {own} into {shared} and make {own} the one line {line}")
    return out


def folder_project_name(folder):
    """The project name maxpm init gives a folder: its name in lower case, other characters as dashes."""
    import re
    return re.sub(r"[^a-z0-9._-]+", "-", Path(folder).name.lower()).strip("-") or "project"


def link_folder(conn, here, project=None, description="", actor=None):
    """The project part of maxpm init: create or link the project of a folder. project names one (it may
    move from another folder); else a project already linked here is kept, or one named after the folder
    is made. Returns (name or None when projects are linked here already, linked_here, lines)."""
    here = Path(here).resolve()
    lines = []
    linked = core.projects_for_dir(conn, here)
    linked_here = [n for n in linked if Path(core._project(conn, n)["path"]) == here]
    if project:
        name = project
    elif linked_here:
        name = None
        lines.append(f"projects already linked to this folder: {', '.join(linked_here)}")
    else:
        name = folder_project_name(here)
    if name:
        exists = conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone()
        if exists:
            core.project_path(conn, name, str(here), actor, move=bool(project))
            lines.append(f"project {name}: linked to {here}")
        else:
            core.project_add(conn, name, notes=description, actor=actor, path=str(here))
            lines.append(f"project {name}: created and linked to {here}")
        if description and exists:
            core.project_describe(conn, name, description, actor)
    return name, linked_here, lines


def init_folder(args):
    conn = core.connect()
    here = Path.cwd()
    name, linked_here, lines = link_folder(conn, here, args.project, args.description, args.actor)
    if args.tracker:
        for n in [name] if name else linked_here:
            core.project_tracker(conn, n, args.tracker, args.actor)
            lines.append(f"project {n}: tracker {args.tracker}")
    # AGENTS.md for Codex, OpenCode, and the other agents that read it; CLAUDE.md imports it for Claude Code.
    if args.file:
        lines += [_append_block(Path(f)) for f in args.file]
    else:
        move = args.move
        if move is None and instructions_layout(here) == "claude_only" and sys.stdin.isatty():
            from . import agents
            own, shared, line, _ = agents.instruction_files()
            readers = (agents.shared_readers() or ["Other agents"])[0]  # the first one (Codex): the line stays short
            ans = input(f"{own} holds this folder's rules; {readers} and other agents read {shared}. Move the rules "
                        f"to {shared} and make {own} import it ({line}), so every agent reads them? [Y/n] ")
            move = ans.strip().lower() in ("", "y", "yes")
        lines += setup_instructions(here, move)
    shown = name or linked_here[0]
    if not core._project(conn, shown)["notes"]:
        lines.append(f'next: describe it for agents: maxpm project describe {shown} "what it covers, where, what helps"')
    if not core.setting(conn, "tracker", project_id=core._project(conn, shown)["id"]):
        lines.append(f'if it uses an issue tracker: maxpm project tracker {shown} "github owner/repo via gh"')
    lines += look_setup_lines(shown, core.look_setup(conn, shown))
    lines.append(f"next: add work (maxpm add {shown} \"...\") or open an agent here and say go")
    print("\n".join(lines))
    return 0


def _expert_cmd(conn, a, actor):
    """maxpm expert start|adopt|end|list|notes (#4955)."""
    e = a.ecmd or "list"
    if e == "list":
        from . import sessions
        return {"experts": [dict(x, cache=sessions.cache_state(conn, x["name"])) for x in core.expert_list(conn)]}
    if e == "fresh":
        if in_sandbox():
            core._expert(conn, a.name)
            return {"expert_started": {**ask_server(conn, "expert_fresh", {"name": a.name, "new": a.new}, actor,
                                                    timeout=40), "via_serve": True}}
        from . import server
        return {"expert_started": server.fresh_expert(conn, a.name, actor, new=a.new)}
    if e == "adopt":
        if not actor:
            raise RiverError("run it in the session that becomes the expert, with its name: maxpm --as <name> expert "
                             "adopt --topic \"...\"")
        held = conn.execute("SELECT id, title, project_id FROM items WHERE assignee=? AND status IN "
                            "('in_progress','held') ORDER BY tier='expert' DESC, id LIMIT 1", (actor,)).fetchone()
        if held is None and not a.topic:
            raise RiverError("an expert has a task: claim the task you talk through with the person first, or give "
                             "--topic \"...\" to add an expert-tier task with that title")
        if held is None:
            project = a.project or next(iter(core.projects_for_dir(conn, os.getcwd())), None)
            if not project:
                raise RiverError("name the project of the task: --project <name>")
            iid = core.item_add(conn, project, a.topic, actor=actor, tier="expert")["id"]
            core.expert_make(conn, actor, a.topic, project, actor)
            core.claim(conn, iid, actor)
        else:
            core.item_edit(conn, held["id"], actor=actor, tier="expert")
            core.expert_make(conn, actor, held["title"], core._project_name(conn, held["project_id"]), actor)
        return {"adopted": core.expert_view(conn, core._agent(conn, actor))}
    if e == "request":
        return core.expert_request(conn, a.topic, actor, a.model, a.person, a.name, a.context, a.item, a.project,
                                   os.getcwd(), a.blocked)
    if e in ("propose", "proposals", "accept", "dismiss"):
        if e == "proposals":
            return {"expert_proposals": core.expert_proposals(conn)}
        if e == "dismiss":
            return {"expert_proposal": core.expert_proposal_end(conn, a.id, "dismissed", actor, a.why)}
        if e == "accept":
            if in_sandbox():
                return {"expert_started": {**ask_server(conn, "expert_accept", {"id": a.id}, actor, timeout=40),
                                           "via_serve": True}}
            from . import server
            return {"expert_started": server.accept_expert(conn, a.id, actor)}
        summary = a.summary
        if a.summary_file:
            if summary:
                raise RiverError("give the summary once: --summary \"...\" or --summary-file <path>")
            summary = sys.stdin.read() if a.summary_file == "-" else Path(a.summary_file).expanduser().read_text(encoding="utf-8")
        return {"expert_proposal": core.expert_propose(conn, a.name, a.topic, a.project, a.item, a.model, a.effort,
                                                       summary, a.source, a.canvas, a.session, actor)}
    if e == "end":
        if in_sandbox():
            core._expert(conn, a.name)
        return {"ended_expert": core.expert_end(conn, a.name, actor, a.reason, a.done)}
    if e == "notes":
        topic, project = core.expert_topic_of(conn, a.name)
        if topic is None:
            core._expert(conn, a.name)  # it was never an expert: the error says how to make one
        canvas = core.expert_notes_name(a.name)
        if a.put:
            body = sys.stdin.read() if a.put == "-" else Path(a.put).expanduser().read_text(encoding="utf-8")
            if not project:
                raise RiverError(f"{a.name} has no project for its notes; adopt it again with --project <name>")
            core.canvas_put(conn, canvas, body, actor or a.name, kind="md", title=f"Notes of the expert {a.name}",
                            project=project, note="the expert's notes")
        try:
            got = core.canvas_show(conn, canvas, project)
        except RiverError:
            got = None
        return {"expert_notes": {"name": a.name, "canvas": canvas, "topic": topic,
                                 "text": (got or {}).get("shown", {}).get("body"),
                                 "version": (got or {}).get("shown", {}).get("version")}}
    # start: it opens a terminal, so a sandboxed CLI asks the running maxpm serve, as maxpm launch does.
    summary = a.summary
    if a.summary_file:
        if summary:
            raise RiverError("give the summary once: --summary \"...\" or --summary-file <path>")
        summary = sys.stdin.read() if a.summary_file == "-" else Path(a.summary_file).expanduser().read_text(encoding="utf-8")
    args = {"name": a.name, "topic": a.topic, "project": a.project, "resume": a.resume, "agent": a.agent,
            "model": a.model, "effort": a.effort, "item": a.item, "summary": summary,
            "sources": a.source, "canvases": a.canvas, "sessions": a.session}
    if in_sandbox():
        return {"expert_started": {**ask_server(conn, "start_expert", args, actor, timeout=40), "via_serve": True}}
    from . import server
    return {"expert_started": server.start_expert(conn, a.name, a.topic, a.project, a.resume, agent=a.agent, actor=actor,
                                                  model=a.model, effort=a.effort, item=a.item,
                                                  summary=summary, sources=a.source, canvases=a.canvas,
                                                  sessions=a.session)}


_OPS_FIELDS = ("url", "statuses", "body", "command", "pattern", "limit", "window", "period", "every", "fail_after",
               "recover_after", "timeout", "severity", "runbook", "repair", "layer", "cron", "normal", "errors")


def _ops_profile_file(path):
    """A profile file, - for stdin, or the name of a profile that ships with MaximizePM (maxpm ops profile apply
    maximizepm) when no file has that name."""
    if path != "-" and not Path(path).exists():
        builtin = core.ops_builtin_profile(path)
        if builtin is not None:
            return builtin
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise RiverError(f"cannot read the profile {path}: {e.strerror}")
    try:
        return json.loads(text)
    except ValueError as e:
        raise RiverError(f"the profile {path} is no JSON: {e}")


# maxpm ops setup (#5437). SETUP_ASK: a test hook, (question) -> the answer; else input().
SETUP_ASK = None


def _setup_form(a):
    """The form of the check that the options of maxpm ops setup describe, or None when they describe none."""
    if a.form:
        return a.form
    return "url" if a.url else "beat" if a.period else "log" if a.pattern else "cmd" if a.command else None


def _setup_check(conn, scope, name, form, fields, actor, say, anyway=False, why=None, keep=None):
    """One check of the setup: its rights first; then one run in maxpm serve (outside the sandbox; a beat check has
    none), then keep it. keep(passed) asks the person; without it the check is kept when its run passed, or with
    anyway. When the actor may not add it and gives why, it is proposed instead."""
    kind, sc = core._ops_scope(conn, scope)
    name = name or core.ops_default_name(form, fields)
    if not name:
        raise RiverError("a beat check needs a name: the job that reports, for example --name nightly-backup")
    col = core._ops_build(form, fields)  # every value is checked before anything runs
    why_not = core.may_set_check(conn, actor, dict(col, scope_kind=kind, scope=sc))
    if why_not:
        if not why:
            return {"name": name, "kept": False, "refused": why_not}
        p = core.ops_propose(conn, f"{kind}:{sc}", name, form, fields, why, actor)
        return {"name": name, "kept": False, "proposed": True, "summary": p["proposal"]["summary"]}
    result = None
    if form != "beat":
        say(f"  running {sc}/{name} once now (maxpm serve runs it, outside the sandbox) ...")
        limit = core.parse_duration(col["timeout"]).total_seconds()
        result = ask_server(conn, "ops_trial", {"scope": f"{kind}:{sc}", "name": name, "form": form, "fields": fields},
                            actor, timeout=limit + 15, retries=0)["result"]
        say(f"  {'passed' if result['status'] == 'ok' else 'failed'}: {result['summary'] or result['status']}")
        if result["status"] != "ok" and result.get("output"):
            for line in result["output"].splitlines()[-5:]:
                say(f"    {line}")
    passed = result is None or result["status"] == "ok"
    if not (keep(passed) if keep else passed or anyway):
        return {"name": name, "kept": False, "result": result}
    c = core.ops_add(conn, f"{kind}:{sc}", name, form, fields, actor)["check"]
    return {"name": name, "kept": True, "form": form, "scope": sc, "result": result, "summary": core._ops_summary(c)}


def _setup_notice(conn):
    """One test notice to the phone (the ntfy channel), now."""
    from . import notify
    if "ntfy" not in core._channels(core.setting(conn, "notify_channels")):
        return {"sent": False, "why": "your phone gets no notices yet: maxpm notify setup ntfy"}
    r = notify.test(conn, "ntfy")
    return {"sent": r["ok"], "why": r.get("error")}


def _setup_deadman(conn, url, actor):
    """The outside switch: keep the address (ops_deadman_url), then send one beat to it now."""
    core.config_set(conn, "ops_deadman_url", url, actor=actor)
    try:
        code = int((core.OPS_DEADMAN_GET or (lambda u, lim: core._ops_http(u, lim)[0]))(url, 10))
        return {"url": url, "ok": 200 <= code < 300, "status": code}
    except (OSError, ValueError) as e:
        return {"url": url, "ok": False, "error": str(getattr(e, "reason", e))}


def _setup_ask_check(conn, scope, form, actor, ask, yes, say):
    """The plain questions of one form, then _setup_check, which asks whether to keep it."""
    fields = {}
    for key, question, default in core.OPS_SETUP_QUESTIONS[form]:
        value = ask(question, default)
        while not value and default is None:
            value = ask("  an answer is needed. " + question)
        fields[key] = value or None
    name = ask("Which job reports? Its short name, for example nightly-backup" if form == "beat" else
               "A short name for this check", core.ops_default_name(form, fields))
    while not name:
        name = ask("  a name is needed. Which job reports? Its short name")
    if form != "beat":
        fields["every"] = ask("How often should it run?", core.OPS_DEFAULTS["every"])
    fields["severity"] = "page" if yes("Should a fault of this check be able to reach your phone?", False) else "item"
    kind, sc = core._ops_scope(conn, scope)
    why = None
    why_not = core.may_set_check(conn, actor, dict(core._ops_build(form, fields), scope_kind=kind, scope=sc))
    if why_not:
        say("  " + why_not.split(" Ask ")[0])
        if not yes("Propose it to the manager instead?"):
            return {"name": name, "kept": False, "refused": why_not}
        while not why:
            why = ask("Why is it needed? (one line for the manager)")
    r = _setup_check(conn, scope, name, form, fields, actor, say, why=why, keep=lambda passed: yes(
        "Keep this check?") if passed else yes("It failed now. Keep it anyway?", False))
    if r["kept"] and form == "beat":
        say(f"  the job reports each time it runs: maxpm ops beat {sc} {name}")
    return r


def ops_setup(conn, a, actor):
    """maxpm ops setup (#5437): in a terminal, one question at a time; with --yes, or with no terminal, only what the
    options say. Steps: the project or target, the checks (each runs once before it is kept), a test notice to the
    phone, the outside switch."""
    from . import notify
    asking = not a.yes and (SETUP_ASK is not None or sys.stdin.isatty())
    done = {"scope": None, "checks": [], "notice": None, "deadman": None, "profile": None, "asked": asking}

    def say(text=""):
        print(text, flush=True)

    def ask(question, default=None):
        try:
            answer = (SETUP_ASK or input)(f"{question} [{default}] " if default else f"{question} ").strip()
        except EOFError:
            raise RiverError("setup stopped: no answer came. An agent gives the answers as options: "
                             "maxpm ops setup <scope> --yes ... (maxpm ops setup --help)")
        return answer or default or ""

    def yes(question, default=True):
        answer = ask(question + (" (Y/n)" if default else " (y/N)")).lower()
        return default if not answer else answer.startswith("y")

    fields = {k: getattr(a, k) for k in _OPS_FIELDS}
    fields["subject"] = None if a.subject is None else a.subject == "on"
    fields["outside"] = None if a.outside is None else a.outside == "on"
    form = _setup_form(a)
    if not asking:
        if a.profile:
            done["profile"] = _setup_profile(conn, a.profile, a.scope, actor)
            if not (form or a.test_notice or a.deadman_url):
                return {"setup": done}
        if not a.scope:
            raise RiverError("name the project or the deploy target: maxpm ops setup <scope> --yes ... (in a "
                             "terminal it asks)")
        if not (form or a.test_notice or a.deadman_url):
            raise RiverError("nothing to set up: give a check (--url; --period with --name; --command; --command "
                             "with --pattern), --test-notice, or --deadman-url")
        done["scope"] = "{}:{}".format(*core._ops_scope(conn, a.scope))
        if form:
            done["checks"].append(_setup_check(conn, a.scope, a.name, form, fields, actor, say, a.anyway,
                                               a.why_propose))
        if a.test_notice:
            done["notice"] = _setup_notice(conn)
        if a.deadman_url:
            done["deadman"] = _setup_deadman(conn, a.deadman_url, actor)
        return {"setup": done}

    say("MaximizePM can watch a project's production and tell you when something breaks.")
    say("Enter takes the answer in [brackets].")
    if a.profile:
        done["profile"] = _setup_profile(conn, a.profile, a.scope, actor)
    # MaximizePM itself, by the profile that ships with it (#5442): offered first while its project has no check.
    own = core.ops_builtin_profile("maximizepm")
    own_project = own["scope"].split(":", 1)[1] if own else None
    if own and conn.execute("SELECT 1 FROM checks WHERE scope_kind='project' AND scope=?", (own_project,)).fetchone():
        own = None
    scope = a.scope
    while True:
        if not scope:
            names = ([f"project:{r['name']}" for r in conn.execute(
                "SELECT name FROM projects WHERE archived=0 AND name NOT LIKE 'deploy-%' ORDER BY rank, name")]
                + [f"target:{r['name']}" for r in conn.execute("SELECT name FROM targets ORDER BY name")])
            if own:
                say(f"  0. MaximizePM itself: {len(own['checks'])} checks of maxpm serve, the relay, sessions that "
                    f"stop, notices that fail, errors in its log and a slow page (the profile maximizepm)")
            for i, n in enumerate(names, 1):
                say(f"  {i}. {n.replace(':', ' ', 1)}")
            pick = ask("Which project or deploy target should MaximizePM watch? (a number or a name)")
            if own and pick == "0":
                try:
                    done["profile"] = _setup_profile(conn, "maximizepm", None, actor)
                    say(f"  added {len(done['profile']['add'])} checks to the project {own_project}")
                    pick = f"project:{own_project}"
                except RiverError as e:
                    say(f"  {e}")
                    pick = ""
                own = None
            scope = names[int(pick) - 1] if pick.isdigit() and 0 < int(pick) <= len(names) else pick
        try:
            done["scope"] = "{}:{}".format(*core._ops_scope(conn, scope))
            break
        except RiverError as e:
            say(f"  {e}")
            scope = None
    say(f"What should MaximizePM watch in {done['scope'].split(':', 1)[1]}? Add as many checks as you like.")
    numbers = {str(i): f for i, (f, _) in enumerate(core.OPS_SETUP_FORMS, 1)}
    while True:
        for i, (f, text) in enumerate(core.OPS_SETUP_FORMS, 1):
            say(f"  {i}. {text}")
        pick = ask(f"Which one? (1-{len(numbers)}, Enter: no more checks)")
        if not pick:
            break
        f = numbers.get(pick) or (pick if pick in numbers.values() else None)
        if f is None:
            say(f"  answer 1 to {len(numbers)}, or press Enter")
            continue
        try:
            done["checks"].append(_setup_ask_check(conn, done["scope"], f, actor, ask, yes, say))
        except RiverError as e:
            say(f"  {e}. This check was not kept; try again")
    if "ntfy" not in core._channels(core.setting(conn, "notify_channels")):
        say("Your phone gets no notices from MaximizePM yet.")
        if yes("Set up notices to your phone now (the free ntfy app)?"):
            for line in notify.setup_ntfy(conn, actor=actor)["subscribe"]:
                say(f"  {line}")
            ask("Press Enter when the phone has subscribed")
    if "ntfy" in core._channels(core.setting(conn, "notify_channels")) and yes(
            "Send a test notice to your phone now?"):
        done["notice"] = _setup_notice(conn)
        say("  sent: look at your phone" if done["notice"]["sent"] else f"  not sent: {done['notice']['why']}")
    current = core.setting(conn, "ops_deadman_url")
    if current:
        say(f"The outside switch is on: maxpm serve sends a beat to {current} every "
            f"{core.setting(conn, 'ops_deadman_every')}.")
    else:
        say("When this computer is off or offline, nothing on it can tell you. A heartbeat service outside it can:")
        say("maxpm serve sends it a beat each minute, and the service tells you when the beats stop. Any service with")
        say("a ping address works; Healthchecks.io has a free plan, and it can send its alerts to your ntfy topic.")
        url = ask("The ping address (Enter: skip for now)")
        if url:
            done["deadman"] = _setup_deadman(conn, url, actor)
            d = done["deadman"]
            say("  the first beat arrived" if d["ok"] else
                f"  the first beat failed ({d.get('error') or 'HTTP ' + str(d.get('status'))}); maxpm serve tries "
                f"again each minute")
    return {"setup": done}


def _setup_profile(conn, name, scope, actor):
    """Apply a profile in maxpm ops setup. A profile that ships with MaximizePM names its own project: setup adds
    that project when the queue has none of that name."""
    prof = _ops_profile_file(name)
    target = scope or prof.get("scope") or ""
    kind, _, pname = target.partition(":") if target.startswith(("project:", "target:")) else ("project", "", target)
    if kind == "project" and pname and core.ops_builtin_profile(name) is not None and not conn.execute(
            "SELECT 1 FROM projects WHERE name=?", (pname,)).fetchone():
        core.project_add(conn, pname, notes="MaximizePM on this computer: the checks that watch it "
                         f"(maxpm ops profile apply {name})", actor=actor)
    res = core.ops_profile_apply(conn, prof, scope, True, actor)
    res["outside"] = sum(1 for c in prof.get("checks", []) if c.get("outside"))
    return res


def _dispatch_ops(conn, a, actor):
    if a.ocmd == "record":
        return core.ops_detections(conn, a.project, a.kind, a.since, a.check, a.n)
    if a.ocmd == "list":
        return core.ops_list(conn, a.scope)
    if a.ocmd == "show":
        return core.ops_show(conn, a.scope, a.name)
    if a.ocmd == "history":
        return core.ops_history(conn, a.scope, a.name, a.n)
    if a.ocmd == "beat":
        return core.ops_beat(conn, a.scope, a.name, actor)
    if a.ocmd == "run":
        core.ops_find(conn, a.scope, a.name)  # a wrong name says so here, not after a trip to maxpm serve
        return ask_server(conn, "ops_run", {"scope": a.scope, "name": a.name}, actor)
    if a.ocmd == "deps":
        return core.ops_deps(conn, a.project, a.add, actor)
    if a.ocmd == "cause":
        def run_check(c):
            import time
            res = ask_server(conn, "ops_run", {"scope": c["scope"], "name": c["name"]}, actor)
            wait = core.parse_duration(c["timeout"]).total_seconds() + 15
            end = time.monotonic() + wait
            while time.monotonic() < end:
                r = conn.execute("SELECT * FROM ops_runs WHERE id=? AND ended_at IS NOT NULL", (res["run"],)).fetchone()
                if r:
                    return dict(r)
                time.sleep(0.5)
            raise RiverError(f"the run of {c['scope']}/{c['name']} did not end within {int(wait)}s: maxpm ops history "
                             f"{c['scope']} {c['name']}")
        return core.ops_cause(conn, a.id, a.cause, a.proof, a.action, a.expect, a.check, actor, run_check)
    if a.ocmd == "kinds":
        return {"kinds": core.ops_kinds()}
    if a.ocmd == "kind":
        return core.ops_kind_set(conn, a.id, a.kind, a.why, a.message, actor)
    if a.ocmd == "noise":
        if a.oncmd == "add":
            return core.ops_noise_add(conn, a.scope, a.name, a.reason, a.pattern, a.subject, a.until, a.fault, actor)
        if a.oncmd == "list":
            return {"rules": core.ops_noise_list(conn, a.scope, a.all)}
        return core.ops_noise_rm(conn, a.rule, actor, a.note)
    if a.ocmd == "system":
        if a.oscmd == "show":
            return core.ops_system_show(conn, a.project, a.version)
        if a.oscmd == "edit":
            sets = {}
            for kv in a.sets:
                k, eq, v = kv.partition("=")
                if not eq:
                    raise RiverError(f"--set takes PART.FIELD=TEXT, not {kv!r}")
                sets[k.strip()] = v
            body = None
            if a.file:
                text = sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8")
                try:
                    body = json.loads(text)
                except ValueError as e:
                    raise RiverError(f"{a.file} is not JSON: {e}")
            if body is None and not sets:
                raise RiverError("give the changes: --set PART.FIELD=TEXT (repeat it), or --file <description.json>")
            if body is not None and sets:
                raise RiverError("give --set or --file, not both")
            return core.ops_system_edit(conn, a.project, sets, body, a.why, actor)
        if a.oscmd == "accept":
            return core.ops_system_accept(conn, a.project, actor, a.note)
        return core.ops_system_reject(conn, a.project, actor, a.note)
    if a.ocmd == "profile" and a.opcmd == "export":
        res = core.ops_profile_export(conn, a.scope)
        if a.out:
            Path(a.out).write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
        return {"profile": res, "out": a.out}
    if a.ocmd == "profile" and not a.yes:
        return core.ops_profile_apply(conn, _ops_profile_file(a.file), a.scope, False, actor)
    if a.ocmd == "repair":
        if not actor and core.agent_type_from_env(os.environ):
            raise RiverError("name yourself: maxpm --as <name> ops repair ... (the session that holds the fault item)")
        core.ops_find(conn, a.scope, a.name)
        return ask_server(conn, "ops_repair", {"scope": a.scope, "name": a.name, "reason": a.reason}, actor)
    if not actor and core.agent_type_from_env(os.environ):
        raise RiverError(f"name yourself: maxpm --as <name> ops {a.ocmd} ... (an agent may add or change a url or "
                         f"beat check; a person or the manager the others)")
    if a.ocmd == "setup":
        return ops_setup(conn, a, actor)
    if a.ocmd in ("accept", "reject"):
        return (core.ops_accept if a.ocmd == "accept" else core.ops_reject)(conn, a.scope, a.name, actor, a.note)
    if a.ocmd == "profile":
        return core.ops_profile_apply(conn, _ops_profile_file(a.file), a.scope, True, actor)
    if a.ocmd == "visual":  # approve; run and show are plugin lines (run())
        from .plugins import visual
        return visual.approve(conn, a.name, a.shot, actor, a.note)
    if a.ocmd == "rm":
        return core.ops_rm(conn, a.scope, a.name, actor)
    if a.ocmd == "pause":
        return core.ops_pause(conn, a.scope, a.name, a.until, a.resume, actor)
    fields = {k: getattr(a, k) for k in _OPS_FIELDS}
    fields["subject"] = None if a.subject is None else a.subject == "on"
    fields["outside"] = None if a.outside is None else a.outside == "on"
    if a.ocmd == "add":
        return core.ops_add(conn, a.scope, a.name, a.form, fields, actor)
    if a.ocmd == "propose":
        return core.ops_propose(conn, a.scope, a.name, a.form, fields, a.why, actor)
    return core.ops_edit(conn, a.scope, a.name, fields, a.form, actor)


def dispatch(conn, a, actor):
    c = a.cmd
    if c == "schedule":
        if a.schcmd == "add":
            return core.schedule_add(conn, a.project, a.title, cron=a.cron, yearly=a.yearly, lead=a.lead, doer=a.doer,
                                     priority=a.priority, context=a.context, actor=actor)
        if a.schcmd == "list":
            return core.schedule_list(conn, a.project)
        return core.schedule_rm(conn, a.id, actor)
    if c == "ops":
        return _dispatch_ops(conn, a, actor)
    if c == "project":
        if a.pcmd == "add":
            return core.project_add(conn, a.name, a.rank, a.notes, actor, a.path, a.target, a.result_look)
        if a.pcmd == "rank":
            return core.project_rank(conn, a.name, a.rank, actor)
        if a.pcmd == "path":
            return core.project_path(conn, a.name, a.path, actor, move=a.move)
        if a.pcmd == "describe":
            return core.project_describe(conn, a.name, a.text, actor)
        if a.pcmd == "show":
            return core.project_show(conn, a.name)
        if a.pcmd == "target":
            return core.project_target(conn, a.name, a.target, actor)
        if a.pcmd == "tracker":
            return core.project_tracker(conn, a.name, a.text, actor)
        if a.pcmd == "rename":
            return core.project_rename(conn, a.name, a.new, actor)
        if a.pcmd == "archive":
            return core.project_archive(conn, a.name, actor)
        if a.pcmd in ("restore", "unarchive"):
            return core.project_restore(conn, a.name, actor)
        if getattr(a, "archived", False):
            return {"archived_projects": core.project_archived(conn)}
        return core.project_list(conn)
    if c == "browser":
        return _browser_cmd(conn, a, actor)
    if c == "canvas":
        return _canvas_dispatch(conn, a, actor)
    if c == "secret":
        return _secret_cmd(conn, a, actor)
    if c == "realquicksite":
        if a.rqcmd == "status":
            return core.rqs_status(conn)
        if a.rqcmd == "disconnect":
            return core.rqs_disconnect(conn)
        return ask_server(conn, "rqs_connect", {}, actor=actor)
    if c == "goal":
        g = a.gcmd
        if g == "add":
            return core.goal_add(conn, a.project, a.name, a.outcome, a.done_when, actor, a.rank, a.shared, a.parent)
        if g == "list":
            return core.goal_list(conn, a.project, a.all)
        if g == "show":
            return core.goal_show(conn, a.name)
        if g == "rank":
            return core.goal_rank(conn, a.name, a.rank, actor)
        if g == "edit":
            return core.goal_edit(conn, a.name, a.outcome, a.done_when, a.rename, actor, a.shared)
        if g in ("own", "take"):
            return core.goal_own(conn, a.name, actor, a.lease)
        if g == "give":
            return core.goal_give(conn, a.name, a.to, actor, a.no_handoff)
        if g == "release":
            return core.goal_release(conn, a.name, actor, a.no_handoff)
        if g == "base":
            return core.goal_base(conn, a.name, actor, a.ready, a.clear, core.claude_session_from_env(),
                                  os.environ.get("MAXPM_MODEL"), os.getcwd())
        if g == "handoff":
            if a.text is not None and a.file:
                raise RiverError("give the handoff as text or --file, not both")
            text = a.text
            if a.file:
                try:
                    text = Path(a.file).expanduser().read_text(encoding="utf-8")
                except OSError as e:
                    raise RiverError(f"cannot read {a.file}: {e.strerror}")
            return core.goal_handoff(conn, a.name, text, actor, a.version)
        if g == "done":
            return core.goal_done(conn, a.name, a.result, actor, a.drop_open)
        if g == "reopen":
            return core.goal_reopen(conn, a.name, actor)
    if c == "expert":
        return _expert_cmd(conn, a, actor)
    if c == "launch":
        from . import server
        opts = {}
        for o in a.option:
            k, sep, v = o.partition("=")
            if not sep:
                raise RiverError(f"--option {o!r} needs the form name=value, for example remote_control=off")
            opts[k.strip()] = v.strip()
        if a.dry_run:
            t = core.launch_target(conn, a.project, a.agent, a.item, a.model, a.effort,
                                   spread=a.project is None and a.item is None, options=opts or None, actor=actor,
                                   prompt=a.prompt)
            return {**t, "dry_run": True, "would_push_to": None if t["custom_prompt"] else
                    core.waiting_agent_for(conn, t["project"], t["item"]["id"], a.model)}
        if in_sandbox():
            # A sandbox blocks the tmux socket for this command. maxpm serve runs outside it and opens the
            # session, as for the page's Start and Dispatch.
            if a.prompt is not None:
                core.custom_prompt(a.prompt)  # refuse here, before the request
            choice = {"agent": a.agent, "model": a.model, "effort": a.effort, "options": opts, "prompt": a.prompt}
            res = (ask_server(conn, "dispatch_item", {"id": a.item, **choice}, actor, timeout=40) if a.item is not None
                   else ask_server(conn, "launch_agent", {"project": a.project, **choice}, actor, timeout=40))
            return {**res, "via_serve": True}
        if a.item is not None:
            return server.dispatch_item(conn, a.item, agent=a.agent, actor=actor, model=a.model, effort=a.effort,
                                        options=opts or None, prompt=a.prompt)
        return server.launch_agent(conn, a.project, agent=a.agent, actor=actor, model=a.model, effort=a.effort,
                                   options=opts or None, prompt=a.prompt)
    if c == "view":
        from . import server
        if a.orphans:
            return server.tmux_orphans_view(end=a.tidy)
        return server.tmux_view(a.layout, a.tidy, conn)
    if c == "interrupt":
        # maxpm serve types the keys: a sandboxed session cannot reach tmux, and the page does the same (#5547).
        return ask_server(conn, "agent_interrupt", {"agent": a.agent, "text": a.message}, actor)
    if c == "stop":
        if a.kill:
            from . import server
            return server.kill_agent(conn, a.agent, a.reason, actor)  # also an agent with only a tmux pane (#5278)
        return core.stop_agent(conn, a.agent, a.reason, actor)
    if c == "queue":
        if a.qcmd == "add":
            return core.queue_add(conn, a.agent, a.id, a.message, a.first, a.before, actor)
        if a.qcmd == "list":
            if not (a.agent or actor):
                raise RiverError("name the agent, or yourself with --as <name>")
            return core.queue_list(conn, a.agent or actor)
        if a.qcmd == "remove":
            return core.queue_remove(conn, a.agent, a.ref, actor)
        return core.queue_move(conn, a.agent, a.id, a.before, a.after, actor)
    if c == "target":
        if a.tcmd == "add":
            return core.target_add(conn, a.name, a.description, actor)
        if a.tcmd == "describe":
            return core.target_describe(conn, a.name, a.text, actor)
        if a.tcmd == "show":
            return core.target_show(conn, a.name)
        if a.tcmd == "rename":
            return core.target_rename(conn, a.name, a.new, actor)
        if a.tcmd == "deployer":
            return core.target_show(conn, a.name) if a.mode is None else core.target_deployer(conn, a.name, a.mode, actor)
        if a.tcmd == "watch":
            return core.target_show(conn, a.name) if not a.times else core.target_watch_times(conn, a.name,
                                                                                              " ".join(a.times), actor)
        if a.tcmd == "cadence":
            return core.target_show(conn, a.name) if a.cadence is None else core.target_cadence(conn, a.name, a.cadence, actor,
                                                                                                    a.reason)
        if a.tcmd == "hook":
            if not actor and (a.command is not None or a.clear) and core.agent_type_from_env(os.environ):
                raise RiverError("name yourself: maxpm --as <name> target hook ... (only a person or the manager "
                                 "sets, changes, or removes a hook)")
            return core.target_hook(conn, a.name, a.event, a.command, a.clear, actor)
        if a.tcmd == "release-now":
            return core.release_now(conn, a.name, a.reason, actor)
        if a.tcmd == "cut":
            return core.target_cut(conn, a.name, a.rev, actor)
        if a.tcmd == "own":
            return core.target_own(conn, a.name, actor, a.takeover)
        if a.tcmd == "release":
            return core.target_release(conn, a.name, actor)
        if a.tcmd == "give":
            return core.target_give(conn, a.name, a.to, actor)
        return core.target_list(conn)
    if c == "add":
        if a.mode and a.blocks is None:
            raise RiverError("--keep and --release go with --blocks <id>")
        if a.plan_file:
            if len(a.words) > 1:
                raise RiverError("with --from, give at most a project: maxpm add [project] --from plan.md")
            text = sys.stdin.read() if a.plan_file == "-" else Path(a.plan_file).expanduser().read_text(encoding="utf-8")
            project = a.words[0] if a.words else core.project_for_add(conn, os.getcwd(), None)
            return core.add_plan(conn, project, text, actor, a.priority, a.doer, a.dry_run)
        if a.dry_run:  # a one-item add has no preview: it would add the item
            raise RiverError("--dry-run goes with --from <plan file>; a one-item maxpm add has no dry run. "
                             "Leave out --dry-run to add the item")
        if not a.words:
            raise RiverError("give a title: maxpm add [project] \"title\"   (or a plan file: maxpm add --from plan.md)")
        if len(a.words) > 2:
            raise RiverError("put the title in quotes: maxpm add [project] \"title\"")
        if len(a.words) == 2:
            project, title = a.words
        else:
            title = a.words[0]
            project = core.project_for_add(conn, os.getcwd(), a.blocks if a.blocks is not None else a.found_during)
        res = core.item_add(conn, project, title, a.priority, a.notes, a.doer, a.after, actor,
                            a.context, a.touches, a.check, a.blocks, a.mode, a.found_during, a.feeds, a.due,
                            [] if a.no_goal else a.goal, core.parse_refs(a.ref, a.ref_url), _models(a),
                            backlog=a.backlog, check_similar=True, new=a.new, mine=a.mine, tier=a.tier,
                            kind=a.kind, short=a.short)
        if a.no_goal_context:
            core.item_goal_context(conn, res["id"], False, actor)
        return res
    if c == "edit":
        if a.unreserve:
            core.cancel_push(conn, a.id, actor)
        if a.goal_context is not None:
            core.item_goal_context(conn, a.id, a.goal_context, actor)
        return core.item_edit(conn, a.id, a.title, a.notes, a.doer, a.project, actor, a.context, a.touches, a.check,
                              a.due, a.goal, a.untag, core.parse_refs(a.ref, a.ref_url), a.unref, _models(a),
                              tier=a.tier, kind=a.kind, short=a.short, changed=a.changed)
    if c == "list":
        return core.item_list(conn, a.project, a.status, a.all, a.goal, a.ref)
    if c == "show":
        def show():
            res = core.item_show(conn, a.id)
            res["related"] = core.related(conn, a.id)
            res["artifacts"] = _item_artifacts(conn, res)
            return res
        from . import space
        if space.is_archived(conn, a.id):  # read as if it were back, then roll back (#5712)
            return dict(space.with_archive(conn, show, [a.id]), archived=True)
        return show()
    if c == "artifacts":
        return artifacts_cmd(conn, a)
    if c == "search":
        return core.search(conn, " ".join(a.words), a.project, a.status, a.since, a.limit, a.kinds)
    if c == "duplicates":
        return {"duplicates": core.duplicates(conn, a.project)}
    if c == "merge":
        return core.merge(conn, a.keep, a.other, a.note, actor)
    if c == "status":
        from . import relay
        return {**core.status(conn, a.recent), "relay": relay.saved_status()}
    if c in ("usage", "health") and a.archive:  # the archived items too, read on demand (#5712)
        if c == "usage" and a.measure:
            raise RiverError("--measure writes the item's usage: run it without --archive")
        from . import space
        a.archive = False
        return space.with_archive(conn, lambda: dispatch(conn, a, actor))
    if c == "usage":
        return core.usage_report(conn, a.project, a.since, a.measure)
    if c == "health":
        return core.health(conn, a.since, a.until, a.project, a.by)
    if c == "change":
        if a.chcmd == "add":
            return core.change_add(conn, a.name, a.at, a.note, actor)
        if a.chcmd == "remove":
            return core.change_remove(conn, a.id, actor)
        return core.change_list(conn)
    if c == "needs-you":
        return core.needs_you(conn, a.human, a.all)
    if c == "prompt":
        person = a.person or (actor if actor and conn.execute(
            "SELECT 1 FROM agents WHERE name=? AND kind='human'", (actor,)).fetchone() else None)
        if a.all:
            return {"prompt": core.prompt_for_all(conn, person)}
        if a.id is None:
            raise RiverError("give an item id, or --all")
        return {"prompt": core.prompt_for(conn, a.id, person)}
    if c == "notify":
        from . import notify
        if a.ncmd == "test":
            return notify.test(conn, a.channel)
        if a.ncmd == "status":
            return notify.status(conn)
        if a.ncmd == "setup":
            return notify.setup_ntfy(conn, a.url, a.token, actor, a.open)
        if a.once or a.now:
            return {"results": notify.run(conn, now_=a.now)}
        import threading
        print(f"maxpm notify: sending every {core.setting(conn, 'notify_interval')} (ctrl-c stops)", flush=True)
        try:
            notify.loop(threading.Event())
        except KeyboardInterrupt:
            pass
        return {"results": []}
    if c == "log":
        return core.completed(conn, a.project, None if a.since == "all" else a.since)
    if c == "go":
        http_chat = core.HTTP_CHAT.get()
        if http_chat:  # a chat over MCP HTTP: maxpm serve's environment and processes are not the chat's
            res = core.go(conn, os.getcwd(), actor, a.project, a.role, a.session, None, None, chat=a.chat)
            core.set_via(conn, res["agent"], http_chat)
            return res
        res = core.go(conn, os.getcwd(), actor, a.project, a.role, a.session, os.environ.get("MAXPM_FOCUS"), a.model,
                      chat=a.chat, agent_type=core.agent_type_from_env(os.environ))
        # The platform's own messaging reaches this session at once (native_message): record its address.
        core.set_native(conn, res["agent"], *core.native_from_env(conn, os.environ))
        _record_process(conn, res["agent"])
        return res
    if c == "plan":
        return {"plan_removed": True}
    if c == "manage":
        if a.watch:
            if not actor:
                raise RiverError("--watch needs the manager's name: maxpm --as <name> manage --watch")
            platform, address = core.native_from_env(conn, os.environ)
            me = conn.execute("SELECT role, platform, native_address FROM agents WHERE name=?", (actor,)).fetchone()
            if platform and not core.HTTP_CHAT.get() and me and me["role"] == "manager" \
                    and (me["platform"], me["native_address"]) != (platform, address):
                # A manager that started before its platform had a channel gets it with its next watch: then
                # MaximizePM can wake it when its watch stops (#5747).
                core.set_native(conn, actor, platform, address)
            return core.manage_watch(conn, actor, a.step)
        res = core.manage(conn, os.getcwd(), actor, a.takeover)
        if not core.HTTP_CHAT.get():
            core.set_native(conn, res["agent"], *core.native_from_env(conn, os.environ))
        return {**res, "native": core.has_native(conn, res["agent"]), "chat": a.chat}
    if c == "next":
        if actor and not core.HTTP_CHAT.get() and conn.execute("SELECT 1 FROM agents WHERE name=?", (actor,)).fetchone():
            if a.model:
                core.set_agent_model(conn, actor, a.model)
            core.set_agent_type(conn, actor, core.agent_type_from_env(os.environ))
        skipped = []
        res = core.next_item(conn, a.project, a.unblocks, a.claim, actor, a.limit, a.near, a.mine, a.model, skipped)
        if skipped and not a.json:
            print(f"skipped for your model {a.model or core.agent_model(conn, actor)} or agent type "
                  f"{core.session_type(conn, actor)} (min/max model limits, the item's agent):",
                  file=sys.stderr)
            for x in skipped[:5]:
                print(f"  #{x['id']} {_cut(x['title'], 50)}: {x['why']}", file=sys.stderr)
        return res
    if c == "claim":
        m = os.environ.get("MAXPM_MODEL")
        if actor and not core.HTTP_CHAT.get() and conn.execute("SELECT 1 FROM agents WHERE name=?", (actor,)).fetchone():
            if m:
                core.set_agent_model(conn, actor, m)
            core.set_agent_type(conn, actor, core.agent_type_from_env(os.environ))
        return core.claim(conn, a.id, actor)
    if c == "done":
        return core.done(conn, a.id, a.output, actor, a.ship, a.note, a.synced, a.force, a.changed)
    if c == "synced":
        return core.synced(conn, a.id, a.ref, actor)
    if c == "cleanup":
        if a.space or a.dry_run:  # the size limits (#5711)
            from . import space
            return space.run(conn, dry_run=a.dry_run, actor=actor)
        return core.cleanup(conn, a.project, not a.no_git, a.closes, actor)
    if c == "wait":
        return core.wait(conn, os.getcwd(), actor, a.project, a.step, focus=os.environ.get("MAXPM_FOCUS"))
    if c == "check":
        return core.check(conn, a.id, a.result, a.note, actor)
    if c == "ship":
        return core.ship(conn, a.id, actor)
    if c == "review":
        if a.rcmd == "pass":
            return core.review_pass(conn, a.id, a.output, actor, os.getcwd(), confirm=a.confirm, rev=a.rev)
        if a.rcmd == "step":
            if a.scmd == "list":
                return core.review_steps(conn, a.project)
            if a.scmd == "add":
                return core.review_step_add(conn, a.project, a.text, a.run, a.at, actor)
            if a.scmd == "edit":
                return core.review_step_edit(conn, a.id, a.text, a.run, actor)
            if a.scmd == "rm":
                return core.review_step_remove(conn, a.id, actor)
            return core.review_step_move(conn, a.id, a.to, actor)
        return core.review_fail(conn, a.id, a.fixes, a.note, a.project, actor, a.ask)
    if c == "policy":
        if a.polcmd == "list":
            if a.for_role:
                return core.policies_for(conn, [a.project] if a.project else [], a.for_role)
            return core.policy_list(conn, a.project, a.role, a.kind)
        if a.polcmd == "show":
            return core.policy_show(conn, a.id)
        if a.polcmd == "add":
            return core.policy_add(conn, a.text, a.reason, a.source, a.project, a.role, a.item, actor, a.adds_to,
                                   a.folder)
        if a.polcmd == "edit":
            return core.policy_edit(conn, a.id, a.text, a.reason, a.source, a.project, a.no_project, a.role,
                                    a.item, actor, a.adds_to, a.folder, a.no_folder)
        return core.policy_retire(conn, a.id, actor)
    if c == "release":
        if a.id is None:
            if a.note or a.author:
                raise core.RiverError("--note and --author go with an item: maxpm release <id> --note ...")
            return core.report_release(conn, actor)
        return core.release(conn, a.id, a.note, actor, a.author)
    if c == "report":
        if a.hook or not a.state:
            raise core.RiverError("maxpm report takes --state working|idle|blocked; --hook <agent> goes alone "
                                  "(maxpm report --hook claude, with the hook's input on stdin)")
        resume = a.resume[1:] if a.resume[:1] == ["--"] else a.resume
        if resume and a.resume[:1] != ["--"]:
            raise core.RiverError(f"unknown words {' '.join(resume)!r}; the resume command goes after --: "
                                  f"maxpm report --state idle -- <command>")
        return core.report(conn, actor, a.state, a.message, a.session_id, a.seq, resume or None)
    if c == "drop":
        return core.drop(conn, a.id, actor, a.note)
    if c == "takeover":
        return core.takeover(conn, a.id, a.note, actor)
    if c == "undo-takeover":
        return core.undo_takeover(conn, a.id, actor)
    if c == "reopen":
        return core.reopen(conn, a.id, actor)
    if c == "backlog":
        if a.id is None:
            if a.note or a.force:
                raise RiverError("name the item: maxpm backlog <id> --note \"<why not now>\"")
            return {"backlog": core.backlog_list(conn, a.project)}
        if a.project:
            raise RiverError("--project goes with the list (maxpm backlog --project <name>), not with an item")
        return core.backlog(conn, a.id, a.note, actor, a.force)
    if c == "activate":
        return core.activate(conn, a.id, actor, a.note)
    if c == "prio":
        return core.item_prio(conn, a.id, a.priority, actor)
    if c == "move":
        return core.item_move(conn, a.id, a.before, a.after, actor)
    if c == "dep":
        return core.dep_add(conn, a.id, a.on, actor, a.kind, a.mode)
    if c == "keep":
        if a.close is not None:
            return core.close_keep(conn, a.close, a.id, a.note, actor)
        if a.id is None or a.note:
            raise RiverError("name the item: maxpm keep <id>; or keep what a close check listed: "
                             "maxpm keep <id> --close <closed id> --note \"<why it is still needed>\"")
        return core.keep(conn, a.id, actor)
    if c == "push":
        if a.cancel:
            return core.cancel_push(conn, a.id, actor)
        if not a.to:
            raise RiverError("maxpm push <id> --to <agent> (or --cancel to take a push or a reservation back; the same as maxpm edit <id> --unreserve)")
        return core.push(conn, a.id, a.to, a.note, actor)
    if c == "accept":
        if a.message:
            return core.accept_message(conn, a.id, actor)
        return core.accept(conn, a.id, actor)
    if c in ("alert", "ask") or (c == "note" and (len(a.words) > 1 or a.holder_of or a.item or a.goal)):
        if len(a.words) > 2:
            raise RiverError("put the text in quotes: maxpm " + c + " [<agent>] \"...\"")
        to, text = (a.words[0], a.words[1]) if len(a.words) == 2 else (None, a.words[0])
        kind = {"ask": "question", "alert": "alert", "note": "note"}[c]
        return core.message(conn, kind, text, to, a.holder_of, a.item, getattr(a, "file", None), os.getcwd(), actor,
                            a.goal, a.level, a.blocked)
    if c == "decline":
        if a.message:
            return core.decline_message(conn, a.id, a.note, actor)
        return core.decline(conn, a.id, a.note, actor)
    if c == "offer":
        return core.offer(conn, a.text, a.item, a.to, actor, a.goal)
    if c == "give":
        return core.give(conn, a.id, a.to, actor)
    if c == "split":
        return core.split(conn, a.id, a.titles, actor, a.doer)
    if c == "undep":
        return core.dep_remove(conn, a.id, a.on, actor)
    if c == "blocked":
        return core.block(conn, a.id, a.reason, actor, a.until)
    if c == "replanned":
        return core.replanned(conn, a.id, a.note, actor)
    if c == "unblock":
        return core.unblock(conn, a.id, actor)
    if c == "blockers":
        return core.blockers(conn, a.id)
    if c == "send":
        return core.send(conn, a.kind, a.text, a.to or (core.goal_owner(conn, a.goal) if a.goal else None),
                         a.item, a.reply, actor, a.level, a.blocked)
    if c == "answer":
        return core.answer(conn, a.id, a.text, actor, in_chat=a.in_chat, self_answer=a.self_answer)
    if c == "withdraw":
        return core.withdraw(conn, a.id, a.note, actor)
    if c == "forward":
        return core.forward(conn, a.id, a.note, actor, item=a.item, drop=a.drop)
    if c == "inbox":
        if a.wait:
            return core.inbox_wait(conn, actor, a.timeout)
        return core.inbox(conn, actor, a.all, not a.peek)
    if c == "thread":
        if (a.id is None) == (a.item is None):
            raise RiverError("give a message id or --item <id>")
        if a.item is not None:
            return {"thread_id": None, "messages": core.item_messages(conn, a.item)}
        return core.thread(conn, a.id, actor)
    if c == "register":
        res = core.register(conn, a.name, a.human, a.note, a.session)
        if not a.human:
            _record_process(conn, a.name)
        return res
    if c == "session":
        if a.move or a.to:
            if not (a.move and a.to) or a.name:
                raise RiverError("maxpm session --move <session id> --to <agent>")
            if not actor and _session_self(conn):
                raise RiverError("maxpm session --move: name yourself, a person or the manager: maxpm --as <name> "
                                 "session --move <session id> --to <agent>")
            return core.session_move(conn, a.move, a.to, actor)
        if not a.name:
            raise RiverError('maxpm session "<name>" --ref <ref>')
        if len(a.name) == 2 and a.name[0] in ("pin", "unpin") and not a.ref:
            return {"pin": core.session_pin(conn, a.name[1], actor, a.name[0] == "pin")}
        if not actor:
            raise RiverError("set MAXPM_AGENT or pass --as <name>")
        return core.set_session(conn, actor, " ".join(a.name), a.ref)
    if c == "unregister":
        return core.unregister(conn, a.name, actor)
    if c == "now":
        if not actor:
            raise RiverError("set MAXPM_AGENT or pass --as <name>")
        return core.agent_now(conn, actor, " ".join(a.words), a.work)
    if c == "note":
        if not actor:
            raise RiverError("set MAXPM_AGENT or pass --as <name>")
        return core.agent_note(conn, actor, " ".join(a.words))
    if c == "who":
        from . import sessions
        res = [dict(g, cache=sessions.cache_state(conn, g["name"])) if g.get("role") == "expert" else g
               for g in core.who(conn, a.item, a.project, a.file, os.getcwd(), everyone=a.all)]
        if not a.all and a.item is None and a.project is None and a.file is None and not a.json:
            ended = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] - len(res)
            if ended:
                print(f"({ended} stopped or gone agent(s) that hold nothing are hidden: maxpm who --all)", file=sys.stderr)
        return res
    if c == "heartbeat":
        if actor == _session_self(conn):  # a command as another agent records no process for it (#5576)
            _record_process(conn, actor)
        return {"ok": True}
    if c == "capacity":
        return core.capacity(conn)
    if c == "config":
        if a.ccmd == "get":
            if a.key:
                res = {"key": a.key, "value": core.mask(a.key, core.setting(
                    conn, a.key, item_id=a.item, agent=a.agent,
                    project_id=core._project(conn, a.project)["id"] if a.project else None, kind=a.kind))}
                if a.key == "launch_agents" and core.launch_migration_note(conn):
                    res["migrated"] = core.launch_migration_note(conn)
                return res
            return core.config_list(conn)
        if a.ccmd == "set":
            return core.config_set(conn, a.key, a.value, a.project, a.item, a.agent, actor, a.kind)
        return core.config_unset(conn, a.key, a.project, a.item, a.agent, actor, a.kind)
    raise RiverError(f"unknown command {c}")


def relay_line(st):
    """One line about the relay connection, from relay.saved_status()."""
    from . import relay
    if not st["configured"]:
        return "Relay: not connected (maxpm connect)"
    who = f" as {st['account']}" if st.get("account") else ""
    if st["state"] == "connected":
        note = relay.plan_note(st)  # a lapsed relay plan (#4759)
        return f"Relay: connected{who} ({st['url']})" + (f"; {note}" if note else "")
    if st["state"] == "serve not running":
        return f"Relay: signed in{who}, not connected: maxpm serve is not running"
    err = f": {st['error']}" if st.get("error") else ""
    return f"Relay: signed in{who}, {st['state']}{err}"


def connect_command(args):
    from . import relay
    if args.status:
        st = relay.saved_status()
        print(relay_line(st))
        return 0
    if args.off:
        res = relay.disconnect()
        if not res["was_connected"]:
            print("This computer is not connected to the relay.")
        elif res["revoked"]:
            print("Disconnected: the relay revoked this computer's token, and relay.json is deleted.")
        else:
            print(f"relay.json is deleted, but the relay did not confirm the revoke ({res['error']}). "
                  "Disconnect the computer on your relay account page too.")
        return 0
    dev = relay.start_device(args.relay)
    print(f"Approve this computer on the relay:\n  {dev['verification_uri_complete']}\n"
          f"Code: {dev['user_code']} (the page must show the same code)")
    if not args.no_browser:
        try:
            core.open_in_browser(dev["verification_uri_complete"])  # in WSL: the Windows browser (#5180)
        except Exception:
            pass
    print("Waiting for the approval (10 minutes at most)...", flush=True)
    cfg = relay.finish_device(dev, replace=args.replace)
    who = f" as {cfg['account']}" if cfg.get("account") else ""
    print(f"Connected to {cfg['url']}{who}. maxpm serve keeps the connection open: it connects within a few "
          "seconds when it runs (start it with maxpm serve).")
    print("Then open your MaximizePM from any browser: " + (cfg.get("tunnel") or cfg["url"] + " (Open my setup)"))
    return 0


def render_status(res):
    rows = res["projects"]
    if not rows:
        print("(no projects: maxpm project add <name>)")
    else:
        w = max(4, *(len(p["project"]) for p in rows))
        print(f"{'project':<{w}}  {'done':>4} {'open':>4} {'ready':>5} {'working':>7} {'human':>5} {'waiting':>7} "
              f"{'backlog':>7}")
        for p in rows:
            print(f"{p['project']:<{w}}  {p['done']:>4} {p['open']:>4} {p['ready']:>5} {p['in_progress']:>7} "
                  f"{p['human_waiting']:>5} {p['blocked']:>7} {p.get('backlog', 0):>7}"
                  + (f"  [{p['target']}]" if p["target"] else ""))
    print()
    print(f"Recently done ({len(res['recent'])}):" if res["recent"] else "Recently done: nothing yet")
    for it in res["recent"]:
        by = f" by {it['by_agent']}" if it["by_agent"] not in (None, "?") else ""
        print(f"  {it['closed_at'][:16].replace('T', ' ')}  #{it['id']:<4} [{it['project']}] {_cut(it['title'])}{by}")
    print()
    print("Working now:" if res["agents"] else "Working now: nobody")
    for ag in res["agents"]:
        holds = ", ".join(f"#{h['id']} {_cut(h['title'], 60)}" for h in ag["holds"]) or "nothing"
        owns = f"; owns {', '.join(ag['owns'])}" if ag["owns"] else ""
        print(f"  {ag['name']} ({ag['kind']}, {ag['state']}): {holds}{owns}")
    if res.get("ended"):
        print(f"  ({res['ended']} stopped or gone agent(s) that hold nothing: maxpm who --all)")
    if res.get("due"):
        print()
        print("Due dates:")
        for d in res["due"]:
            tag = "OVERDUE " if d["due_state"] == "overdue" else "soon " if d["due_state"] == "soon" else ""
            print(f"  #{d['id']:<4} [{d['project']}] {_cut(d['title'])}  {tag}{d['due_text']}")
    if res.get("unsynced"):
        print()
        print("Tracker not updated yet (post the output, close the issue, then maxpm synced <id>):")
        for u in res["unsynced"]:
            print(f"  #{u['id']:<4} [{u['project']}] {_cut(u['title'])}  " + " ".join(x["ref"] for x in u["refs"]))
    if res["human_waiting"]:
        print()
        print("Waiting on a human:")
        for h in res["human_waiting"]:
            print(f"  #{h['id']:<4} [{h['project']}] {_cut(h['title'])}")
    if res.get("relay") and res["relay"]["configured"]:
        print()
        print(relay_line(res["relay"]))
    print()
    print(f"Open slots: {res['spare_slots']}   sessions with nothing to do: {res['excess_sessions']}")
    for adv in res["advice"]:
        print(" -", adv["text"])


PLAN_GONE = """There is no planner role in MaximizePM (mark, 2026-11-10: 'get rid of planner').
Planning happens in the talk and during the work:
  - Directing the agents and planning with the user: the manager (maxpm manage).
  - A long design talk: an expert (maxpm expert start), which writes the tasks at its end.
  - Work you find while you work: maxpm add "<title>" --found-during <id>.
How to write a good task (dependencies, priorities, context, the backlog): maxpm guide manager, section Writing tasks.
To take work instead: maxpm go"""


def _findings_lines(f, r):
    out = []
    for x in f["stuck"]:
        out.append(f"  STUCK {x['agent']} ({x['state']}, {x['why']}): holds "
                   + (", ".join(f"#{i}" for i in x["holds"]) or "nothing") + (f", {x['queued']} queued" if x["queued"] else "")
                   + f".  Ask it to stop: {r} stop {x['agent']} --reason \"...\"")
    for x in f.get("ends", []):
        # #5776: a session that waits at its prompt for a person's answer should end; the answer starts a fresh one.
        out.append(f"  END IT {x['agent']}: waits at its prompt {x['waited']} for {x['person']}'s answer to question "
                   f"#{x['question']}" + (f" (#{x['item']})" if x["item"] else "") + ". "
                   + (f"maxpm serve ends it {x['ends_after']} after the question, and the answer starts a fresh "
                      f"session. Sooner: answer it ({r} answer {x['question']} \"...\"), or {r} stop {x['agent']} "
                      f"--reason \"waits on an answer\"" if x["ends_after"] else
                      f"It keeps its item or target. Answer it ({r} answer {x['question']} \"...\"), or send it on "
                      f"({r} forward {x['question']})"))
    for x in f["lost_leases"]:
        out.append(f"  LEASE RAN OUT #{x['id']} [{x['project']}] {_cut(x['title'], 50)}: check it, then queue or launch it")
    for x in f["waiting_too_long"]:
        out.append(f"  WAITS TOO LONG {x['agent']} ({x['waited']} in {x['in']}): give it work ({r} queue add "
                   f"{x['agent']} <id>) or stop it ({r} stop {x['agent']} --reason \"no work\")")
    for x in f.get("not_connected", []):
        out.append(f"  NOT CONNECTED {x['agent']}: started {x['since']} ago" + (f" for #{x['item']['id']} {_cut(x['item']['title'], 40)}" if x["item"] else "")
                   + f", and ran no maxpm command: its agent did not start or waits on a prompt in its terminal. "
                   f"Tell the user; then {r} stop {x['agent']} --reason \"did not start\" and launch again")
    for x in f.get("serve_loop", []):
        out.append(f"  SERVE LOOP LATE: maxpm serve made no complete pass for {x['age']}"
                   + (f" (last error: {_cut(x['error'], 80)})" if x["error"] else "")
                   + ", so it does none of its automatic work (tidy, notifications, fresh sessions, reload).  "
                   "maxpm serve --restart")
    for x in f["uncovered"]:
        t = x.get("agent_type")
        out.append(f"  NO {t.upper() + ' ' if t else ''}AGENT in {x['project']}: {x['ready']} ready"
                   + (f" for {t}" if t else "") + f", top #{x['top']['id']} {_cut(x['top']['title'], 40)}."
                   + (f"  {r} launch --item {x['top']['id']}" + (f" --agent \"{x['launch']}\"" if x.get("launch") else
                      f"  (no launch_agents entry runs {t}: add one)") if t else f"  {r} launch --project {x['project']}"))
    for x in f["targets"]:
        out.append(f"  TARGET {x['target']}: owner {x['owner']} is {x['state']}.  {r} target give {x['target']} --to <agent>")
    for x in f["questions"]:
        out.append(f"  QUESTION #{x['id']} from {x['from_agent']} to {x['to_agent']}: {_cut(x['body'], 70)}"
                   + (f"\n    waits for you, not {x['to_agent']}: maxpm answer {x['id']} \"...\", or maxpm forward {x['id']}"
                      if x.get("held_for") else ""))
    for x in f.get("unneeded", []):
        c = x["closes"][0]
        out.append(f"  MAY BE UNNECESSARY #{x['id']} [{x['project']}] {_cut(x['title'], 50)}"
                   + (" (a person's item)" if x["doer"] == "human" else "") + f": {c['why']}"
                   + (f" (and {len(x['closes']) - 1} more)" if len(x["closes"]) > 1 else "")
                   + f".  Close it ({r} done|drop {x['id']} --note \"<why>\"), merge it, or {r} keep {x['id']} "
                   f"--close {c['id']} --note \"<why>\"")
    for x in f.get("broken_rules", []):  # #4976: the role rules that the queue shows broken
        out.append(f"  BROKEN RULE ({x['rule']}): {x['text']}.  " + x["fix"].replace("maxpm ", f"{r} "))
    for x in f.get("goal_checks", []):
        last = x["last"]
        out.append(f"  GOAL DONE? {x['goal']} [{x['project']}] has no open item and "
                   + ("is shared (no owner)" if x["shared"] else "no owner")
                   + (f"; the last one closed was #{last['id']} {_cut(last['title'], 40)}" if last else "")
                   + ". " + x["question"].replace("maxpm ", f"{r} ")
                   + ". Ask the user when only they can tell")
    for x in f.get("unpushed", []):  # #5600: finished work that waits to go out
        out.append(f"  UNPUSHED {x['target']}: main in {x['folder']} is {x['commits']} commit"
                   f"{'s' if x['commits'] != 1 else ''} ahead of origin, the oldest {x['oldest']} old.  Run the tests "
                   f"there and push main (git -C {x['folder']} push origin main), or ask the user for the yes it waits on")
    for x in f.get("untagged", []):
        out.append(f"  NOT TAGGED #{x['id']} {_cut(x['title'], 50)}: it is done, and origin has no tag {x['tag']} "
                   f"or later.  Push main and the tag {x['tag']}: the release waits on them")
    for x in f["human_ready"]:
        if x.get("held_for"):  # held for the manager (#5311): the person does not see it yet
            out.append(f"  PERSON ITEM #{x['id']} {_cut(x['title'], 60)}: waits for you, not the person.  "
                       f"{r} done {x['id']} --note \"<what you did>\" (or drop), or {r} forward --item {x['id']}")
    sent = [x for x in f["human_ready"] if not x.get("held_for")]
    if sent:
        out.append("  WAITS ON THE USER: " + ", ".join(f"#{x['id']} {_cut(x['title'], 40)}" for x in sent[:6]))
        looks = [x for x in sent[:6] if x.get("found_during") and x["title"].lower().startswith("look at")]
        if looks:  # #4838: check what the worker made before the person is asked to look at it
            out.append("  BEFORE YOU RELAY A LOOK AT ITEM, check what the session made: " + ", ".join(
                f"#{x['id']}: maxpm artifacts --item {x['found_during']}" for x in looks))
    return out or ["  nothing needs you now"]


def policy_line(p, width=200):
    """A policy in one line, as a briefing shows it: id, scope, the rule (cut), and its source."""
    t = " ".join(p["text"].split())
    t = t if len(t) <= width else t[:width - 1].rstrip() + "…"
    return (f"  {p['pid']} [{p['scope']}] {t}  ({p['source']}"
            + (f"; adds to {p['adds_to']}" if p.get("adds_to") else "") + ")")


def policy_full(p):
    out = [f"{p['pid']} [{p['scope']}] {p['text']}", f"  source: {p['source']}", f"  reason: {p['reason']}"]
    if p.get("adds_to"):
        out.append(f"  adds to the global policy {p['adds_to']} (maxpm policy show {p['adds_to']})")
    if p.get("items"):
        out.append("  items: " + "; ".join(f"#{i['id']} {i['title']} ({i['status']})" for i in p["items"]))
    if p["kind"] == "global":
        out.append("  ships with MaximizePM (river/policies.json); it changes only by a commit and a release")
        out += [f"  your addition: {x['pid']} [{x['scope']}] {x['text']}" for x in p.get("added_by_user", [])]
    else:
        out.append(f"  added {core.show_time(p['created_at'])}" + (f" by {p['created_by']}" if p.get("created_by") else "")
                   + (f", changed {core.show_time(p['changed_at'])}" if p.get("changed_at") else ""))
    return out


def policy_lines(pols, other=0):
    """The POLICIES block of a briefing: the rules that apply to this session, in short form."""
    if not pols and not other:
        return []
    out = ["POLICIES (rules for this session: global ones ship with MaximizePM, user ones are this queue's own; "
           "the full text and reason: maxpm policy show <id>):"]
    out += [policy_line(p) for p in pols]
    if other:
        out.append(f"  and {other} policy(ies) of projects or other roles: maxpm policy list")
    return out + [""]


def _unanswered_line(m, r):
    """A blocked message that the manager got and did not answer: each watch shows it again, with its age (#5685)."""
    age = core._short(core.now() - core.parse_iso(m["created_at"]))
    body = " ".join(m["body"].split())
    return (f"UNANSWERED blocked #{m['id']} {m['kind']} from {m['from_agent']}, {age}"
            + (f" (#{m['item_id']})" if m["item_id"] else "") + f": {body[:100]}" + ("..." if len(body) > 100 else "")
            + f"  answer: {r} " + (f"answer {m['id']} \"...\"" if m["kind"] == "question"
                                   else f"send note --reply {m['id']} \"...\""))


def render_manage(b):
    me = b["agent"]
    r = f"maxpm --as {me}"
    if "result" in b:  # --watch
        if b["result"] == "stop":
            print("STOP REQUESTED" + (f": {b['stop']['stop_reason']}" if b["stop"].get("stop_reason") else "")
                  + f". Tell the user what you did, then run {r} go to end the session.")
            return
        msgs = b["messages"]
        if b.get("blocked_only"):
            # A blocked message comes alone, with no findings, so a reader of any part of the output sees it: a
            # manager that read only the tail of a long output missed a blocked alert for 18 minutes (#5666).
            names = ", ".join(f"#{m['id']} {m['kind']} from {m['from_agent']}"
                              + (f" about #{m['item_id']}" if m["item_id"] else "") for m in msgs)
            print(f"BLOCKED MESSAGES ({len(msgs)}): the sender stands still until you answer. Only "
                  f"{'this comes' if len(msgs) == 1 else 'these come'} now; the findings"
                  + (f" and {b['later']} other new message(s)" if b.get("later") else "") + " come with the next watch.")
            for m in msgs:
                print(_fmt_msg(m, "  "))
            old = b.get("unanswered") or []
            for m in old:
                print(_unanswered_line(m, r))
            print(f"Answer {names} now" + (f" (and the {len(old)} UNANSWERED above)" if old else "")
                  + f", then start {r} manage --watch again in the background.")
            return
        lines = _findings_lines(b["findings"], r)
        # New messages always open the output, and the last line counts them again: on 2026-11-12 a manager read
        # only the end of a watch that began with CHANGED, and missed a blocked alert for 18 minutes (#5685).
        n, k = len(b["messages"]), sum(1 for m in b["messages"] if m.get("blocked"))
        old = b.get("unanswered") or []
        gone = f"; resolved: {', '.join(b['gone'])}" if b["gone"] else ""
        if n:
            print(f"NEW MESSAGES ({n}" + (f", blocked: {k}" if k else "") + ")" + ("" if b["new"] else gone))
        for m in old:
            print(_unanswered_line(m, r))
        if b["new"]:
            print("CHANGED: " + ", ".join(b["new"]) + gone)
        elif not n and not old:
            print("NOTHING NEW" + gone)
        for m in b["messages"]:
            print(_fmt_msg(m, "  "))
        if b.get("still_open"):
            print(f"  and {b['still_open']} older message(s) still open: {r} inbox")
        if b["new"]:
            print("\n".join(lines))
        elif any(b["findings"].values()) and not b["messages"]:  # the standing findings: reported before
            print(f"  {len(lines)} standing finding(s), reported before: {r} manage lists them")
        seen = [f"{n} new message{'s' if n > 1 else ''} above" + (f" ({k} blocked)" if k else "")] if n else []
        seen += [f"{len(old)} blocked message{'s' if len(old) > 1 else ''} not answered (UNANSWERED above: answer "
                 f"or reply, or act on its item)"] if old else []
        print((f"Act on what is new" + (": " + "; ".join(seen) if seen else "")
               + f", then start {r} manage --watch again in the background."
               if b["new"] or seen else f"Start {r} manage --watch again in the background."))
        return
    out = [f"You are the MaximizePM MANAGER {me}" + (f" (you took over from {b['took_over']})" if b.get("took_over") else "") + "."]
    if b["new_name"]:
        out.append(f"Pass --as {me} on every maxpm command.")
    out += _tmux_line()
    if b.get("release"):
        out += ["", "NEW VERSION: " + b["release"]]
    st = b["status"]
    out += ["", "Projects: " + ", ".join(f"{p['project']} {p['ready']} ready/{p['open']} open"
                                          + (f" ({p['backlog']} in backlog)" if p.get("backlog") else "")
                                          for p in st["projects"][:12] if p.get("open")),
            ""] + policy_lines(b.get("policies") or [], b.get("other_policies") or 0)
    if b.get("handed"):  # #5600: what the last manager did not read comes to this one
        out += ["FROM THE LAST MANAGER (it did not read these; act on them):"] + [_fmt_msg(m, "  ") for m in b["handed"]]
    out += ["NEEDS ATTENTION:"] + _findings_lines(b["findings"], r)
    out += ["",
            "Your job: plan with the user, and keep the other agents working together. You take no items.",
            f"  launch:  {r} launch [--project P | --item N] [--model M] [--effort E] [--dry-run]",
            "  models:  only an expert gets the model of the work it takes up (with no --model, expert start takes it "
            "from the item, and says which and why). Any other item, also one found during Fable work, takes the "
            "default model. Fable (or min model fable) only when the user asks for it. Do not ask the user about a "
            "model choice that the request already implies.",
            f"  experts: a talk that becomes a long talk about one topic goes to an expert: one line to the user, then "
            f"{r} expert start <name> --topic \"...\" --summary-file <path> [--canvas C] [--source S] --session {me}. "
            "The task holds the talk so far; you do no long design talk in your own context.",
            f"  queues:  {r} queue add <agent> <id> [--first] | --message \"...\";  queue list|move|remove",
            f"  message: {r} note|alert|ask <agent> \"...\"",
            f"  stop:    {r} stop <agent> --reason \"...\"   (stuck, or waited longer than wait_too_long {b['wait_too_long']})",
            f"  kill:    {r} stop <agent> --kill --reason \"...\"   emergency only, and only after the user says yes",
            f"  targets: {r} target give <target> --to <agent>",
            *(f"  {_hooks_brief(t, h)}" for t, h in sorted((b.get("hooks") or {}).items())),
            f"  config:  {r} config set launch_agents|default_model|default_effort ...",
            (f"  tidy:    maxpm serve closes the tmux panes of finished sessions every tidy_every {b['tidy_every']}; "
             f"maxpm view --tidy does it now" if core.parse_duration(b["tidy_every"]).total_seconds() else
             f"  tidy:    tidy_every is 0s: run maxpm view --tidy for the tmux panes of finished sessions"),
            (f"In a chat, run {r} manage again when the user asks what changed (manage --watch is for a terminal)."
             if b.get("chat") else
             f"Then watch: keep {r} manage --watch running as a background command (Claude Code: run_in_background "
             f"with a time limit above {b['every']}; a foreground shell: add --step 9m). It exits on a new finding"
             + (f" (after manage_settle {b['settle']}, with the findings that came meanwhile)"
                if b.get("settle") and core.parse_duration(b["settle"]).total_seconds() else "")
             + (", " if b.get("native") else
                f", a message to you (a blocked one or a person's at once, another after manage_wait_high "
                f"{b.get('wait_high', '10m')}, a low one after manage_wait_low {b.get('wait_low', '30m')}; it prints "
                f"every message that waits; a blocked one comes alone, the rest with the next watch), ")
             + f"or after manage_every {b['every']} with one line; start it again each time. It is your only "
               f"watcher: MaximizePM refuses a second wait for your messages."),
            *([] if b.get("chat") or not b.get("native") else [
            "Messages: MaximizePM delivers a blocked one and a person's into this session itself (native_message); "
            "what reached this session does not wake the watch. The watch brings each other message after its wait, "
            "and one that did not reach the session."]),
            "The rules: maxpm guide manager"]
    if b.get("chat"):
        out += [""] + _chat_lines(r, False)[:1] + [
            "  launch still works from a chat: it opens a terminal on this computer with a coding agent."]
    print("\n".join(out))


def _chat_lines(r, has_item=True):
    """What a chat app session (no folder, no shell) does differently: river's words go to the river tool."""
    out = ["IN A CHAT (no folder, no shell): call the maxpm tool with the words after `maxpm` "
           "(for example [\"--as\", \"<you>\", \"show\", \"12\"]). You cannot edit a repository, run a check, "
           "commit, or deploy, so MaximizePM gives this chat only items that need no folder."]
    if has_item:
        out += ["  Do the item here: write, research, or decide with the user. The result goes into the queue,",
                f"  not only the chat: a short result in {r} done <id> --output \"<the result>\"; a long one first in",
                f"  the item's notes ({r} edit <id> --notes \"<the text>\"), then done with a one-line summary.",
                f"  It needs code after all: {r} release <id> --note \"needs a coding agent: <why>\"."]
    out += [f"  What waits on the user: {r} needs-you. Work through it with them: {r} prompt --all prints how,"
            f" item by item ({r} prompt <id> for one)."]
    return out


LOOK_RULE = ("recommend push_first when a deploy target with a review or a release step stands between a push and "
             "the users; recommend ask_first when the project has no target, or a push to main is live at once (a "
             "site, a public repository)")


def look_setup_lines(name, look):
    """What the agent that sets a project up reads about the setting result_look: the project's value, the two
    values, the value to recommend with its reason, and the command. mark: 'It should just be very clear to the
    agent when it's being set up.' (#1677) No project gets its value in silence: where nobody chose, ask_first
    holds, and the agent puts the recommendation to the person (#1682)."""
    value, rec = look["value"], look["recommended"]
    other = "push_first" if value == "ask_first" else "ask_first"
    return [f"look at a finished result: project {name} has result_look {value} "
            + ("(chosen for this project)." if look["chosen"] else "(nobody chose for this project yet)."),
            "  ask_first:  the worker shows the result, asks in the queue, and waits for the person's yes before the push.",
            "  push_first: the worker pushes, closes the item, and checks the result (or adds an agent check); the "
            "person gets only a taste or product decision.",
            f"  recommended here: {rec}, because {look['why']}. Also ask_first when a push to main is live at once "
            f"(a site, a public repository).",
            (f"  change it: maxpm config set result_look {other} --project {name}" if look["chosen"] else
             f"  tell the person the recommendation and its reason; store their answer: "
             f"maxpm config set result_look ask_first|push_first --project {name}")]


def _look_rule(b, it, r, short=False):
    """The briefing lines for a look at the finished result. The item's project decides (setting result_look, #1668):
    push first and check the result (the worker, or an agent check item), or ask first and wait for the yes. Under
    push first, a person item only for a taste or product decision (global policy g1: bring the user nothing the
    manager can answer, #5347)."""
    ask = b.get("result_look") == "ask_first"
    if short:
        if ask:
            return [f"  the user should look at the result: this project asks first: commit, no push, {r} ask <person> "
                    f"\"Look at ...\" --item {it['id']}, then {r} release {it['id']} --note \"<what is left>\" and go: "
                    f"the yes starts a fresh session that pushes"]
        return [f"  the result needs a look: push, done, then check it yourself or {r} add \"Check <result> ...\" "
                f"--found-during {it['id']}; a person item only for a taste or product decision"]
    if ask:
        return [f"  - The user should look at the finished result: this project asks first (setting result_look). "
                f"Commit, do not push, and",
                f"    {r} ask <person> \"Look at <result>: <where>. Push?\" --item {it['id']}   then, not waiting at "
                f"your prompt:",
                f"    {r} release {it['id']} --note \"<what is done, what is left>\" and {r} go. The yes starts a fresh "
                f"session that pushes and closes #{it['id']}."]
    return [f"  - The finished result needs a look: push, {r} done {it['id']}, then check it yourself (open it, take "
            f"screenshots, compare with the item), or",
            f"    {r} add \"Check <result>: <where>, with screenshots\" --found-during {it['id']}   (an agent does "
            f"it).",
            f"    Add a person item (--doer human) only for a taste or product decision that no rule and no earlier "
            f"word of the person settles;",
            f"    the manager sees it first.",
            f"    Ask before the push only for public text, a release to production, a step that deletes data or "
            f"costs money, or when the item says so."]


def _tmux_line():
    """One briefing line for a session outside tmux: the Sessions page of maxpm serve can type only into a tmux pane."""
    if not core.outside_tmux(os.environ):
        return []
    return ["This session runs outside tmux, so the Sessions page of maxpm serve cannot type into it (Open on "
            "claude.ai/code still works). Next time, start sessions from the page or with maxpm launch, or start the agent "
            "inside tmux: tmux new -A -s maxpm"]


def cache_text(c, name=None):
    """One line on an expert's prompt cache (#5023): warm with the minutes left, or cold; the context; and what the
    next message costs."""
    if not c:
        return "cache: not known (no model call in a transcript on this computer)"
    cost = (f"~${c['next_usd']:.2f}" if c.get("next_usd") is not None else f"~{_k(c['next_eq'])} input-token equivalents")
    if c["state"] == "warm":
        head = f"cache: warm, {max(1, c['left'] // 60)}m left ({c['ttl']} cache)"
    else:
        head = f"cache: cold since {core.show_time(c['until'])}"
    return (f"{head}; context {_k(c['context'])} tokens; next message {cost}"
            + (f"; a fresh expert from its handoff costs less: maxpm expert fresh {name}" if c.get("fresh") and name else ""))


def render_expert(res):
    def line(x):
        when = core.show_time(x["talked_at"]) if x.get("talked_at") else "never"
        return (f"  {x['name']}  ({x['state']}; {x.get('model') or 'model not recorded'}; last talk {when}; "
                + (f"ends {core.show_time(x['ends_at'])} with no talk" if x.get("ends_at") else "never ends by itself")
                + ")" + (f"\n    holds {', '.join('#' + str(i) for i in x['holds'])}" if x.get("holds") else "")
                + f"\n    {x['topic']}" + (f"\n    {cache_text(x['cache'], x['name'])}" if "cache" in x else ""))
    if "experts" in res:
        if not res["experts"]:
            print("No experts. Start one: maxpm expert start <name> --topic \"...\" [--project <p>] [--model <m>], or make "
                  "a session one in its chat: maxpm --as <name> expert adopt --topic \"...\"")
        for x in res["experts"]:
            print(line(x))
    elif "expert_request" in res:
        r = res["expert_request"]
        print(f"added the expert-tier task #{r['item']} {r['topic']} ({r['model']})"
              + (f", found during #{r['found_during']}" if r.get("found_during") else ""))
        print(f"  it starts as an expert: {r['start']}   (or maxpm launch --item {r['item']})")
        print(f"  alerted {r['to']} (message #{r['message']}): it starts the expert" if r.get("to") else
              "  nobody to tell: no manager runs and no person is known; ask a person to run the line above")
        print("  Go on with your work; the expert sends its findings when it has them.")
    elif "adopted" in res:
        x = res["adopted"]
        print(f"{x['name']} is now an expert on: {x['topic']}")
        print(f"  MaximizePM does not end it as a worker, and gives it no queue work. Keep its notes: "
              f"maxpm --as {x['name']} expert notes {x['name']} --put <file>. Run maxpm --as {x['name']} go for the rules.")
    elif "ended_expert" in res:
        e = res["ended_expert"]
        print(f"ended the expert {e['ended']}." + (f" Done: {', '.join('#' + str(i) for i in e['done'])}." if e.get("done") else "")
              + (f" Back in the queue: {', '.join('#' + str(i) for i in e['released'])}." if e.get("released") else "")
              + f" Start it again with its context: {e['resume']}   (close its terminal or pane if it is still open)")
    elif "expert_notes" in res:
        n = res["expert_notes"]
        if n["text"]:
            print(f"Notes of {n['name']} (canvas {n['canvas']}, version {n['version']}):\n")
            print(n["text"].rstrip())
        else:
            print(f"{n['name']} has no notes yet. Write them: maxpm --as {n['name']} expert notes {n['name']} --put <file>")
    elif "expert_proposals" in res:
        if not res["expert_proposals"]:
            print("No open expert proposals.")
        for x in res["expert_proposals"]:
            print(f"p{x['id']} {x['name']} ({x['model']}) by {x['manager']}: {x['topic']}")
    elif "expert_proposal" in res:
        x = res["expert_proposal"]
        if x["state"] == "open":
            print(f"proposed p{x['id']}: the expert {x['name']} on {x['model']} ({x['model_why']}): {x['topic']}")
            print(f"  nothing starts yet: your chat on the Sessions tab shows the card (Continue in an expert, Dismiss). "
                  f"Tell the person in one line, and wait for the click. A terminal does it too: maxpm expert accept "
                  f"p{x['id']}   or   maxpm expert dismiss p{x['id']}")
        else:
            print(f"proposal p{x['id']} ({x['name']}: {x['topic']}) is {x['state']}")
    elif "expert_started" in res:
        t = res["expert_started"]
        if t.get("fresh_from"):
            print(f"ended the expert {t['fresh_from']}: its context left the prompt cache (close its terminal or pane "
                  f"if it is still open)")
        print(f"started the expert {t['session_name']} on {t.get('model')} ({t.get('model_why')}) in a tmux pane "
              f"({t['project']}) on #{t.get('task')}: {t['topic']}"
              + (f"; it resumes session {t['resumed']}" if t.get("resumed") else ""))
        if t.get("handover"):
            print(f"the talk so far is in #{t.get('task')}'s context; the expert reads it first. Tell the person in one "
                  f"line, then leave the topic to {t['session_name']}.")


def _fault_lines(f, r):
    """The FAULT block of the go briefing of a fault item (#5432): each check, its state and last runs, and the
    commands of an investigation."""
    if not f:
        return []
    out = [f"  FAULT {f['key']}" + (": several checks of one scope fail at once; find their shared cause" if f["storm"]
                                     else "") + ". You investigate: read and diagnose (POLICIES), then close it with the cause."]
    for c in f["checks"]:
        out.append(f"    check {c['scope']}/{c['name']} ({c['form']}), now {c['state']}"
                   + (f", subject {c['subject']}" if c.get("subject") else ""))
        for x in c["runs"]:
            out.append(f"      {x['ended_at']} {x['status']}: {(x['summary'] or '')[:140]}")
        if c.get("runbook"):
            out.append(f"      runbook: {c['runbook'][:300]}")
        out.append(f"      run it now: {r} ops run {c['scope']} {c['name']}   its runs and changes: "
                   f"{r} ops history {c['scope']} {c['name']}" + ("   (it has a repair command)" if c["repair"] else ""))
    if not f["checks"]:
        out.append("    no check of this key now (removed, or healthy again): read the item's context")
    return out


def render_go(b):
    me = b["agent"]
    r = f"maxpm --as {me}"
    out = []
    out.append(f"You are MaximizePM agent {me}. Role: {b['role'].upper()}. ({b['why']})")
    if core.queue_note():
        out.append(core.queue_note() + ". Tell the user if that is not what they meant.")
    if os.environ.get("MAXPM_FORK_OF") and b["role"] != "context":
        out.append(f"You started as a fork of the context session of goal {os.environ['MAXPM_FORK_OF']}: what it read is "
                   f"your context. Its steps (end the session, no maxpm go) were for it, not for you: you are {me}, "
                   f"and this briefing is yours.")
    if b["new_name"]:
        out.append(f"Your shell may not keep environment variables, so pass --as {me} on every maxpm command.")
    out += _tmux_line()
    # Only Claude Code has session names and ListAgents; it sets CLAUDECODE in the commands it runs.
    if not b.get("session") and os.environ.get("CLAUDECODE"):
        out.append(f"Record your Claude Code session name once, so others can message this session "
                   f"(ListAgents prints 'This session is <name> [<ref>]'): {r} session \"<name>\" --ref <ref>")
    for n in b["projects"]:
        d = b["descriptions"].get(n)
        out.append(f"Project {n}: {d}" if d else f"Project {n}.")
        if (b.get("trackers") or {}).get(n):
            out.append(f"  tracker: {b['trackers'][n]} (items link its issues with --ref)")
    out.append("")
    out += policy_lines(b.get("policies") or [])
    if b.get("asked_open"):
        out.append("OPEN QUESTIONS YOU ASKED: " + core.asked_line(me, b["asked_open"]) + ".")
        out += [f"  #{q['id']} to {q['to_agent']}: {' '.join(q['body'].split())[:160]}" for q in b["asked_open"]] + [""]
    if b.get("focus_note"):
        out += [b["focus_note"], ""]
    if b.get("help_prompt"):
        out += [b["help_prompt"], "",
                f"Your own agent name is {me}; take over a person's item only with their yes: {r} takeover <id> --note \"<how>\"."]
        if b["role"] == "assistant":
            who = b["assistant_for"]
            out += [f"You are the assistant of {who} (#5110): this session takes no queue work. Work they want done goes "
                    f"into the queue ({r} add \"<title>\" --found-during <id>), and a worker session takes it. MaximizePM "
                    f"never ends you as it ends a worker (no lease, no idle_end, no wait): stay at your prompt between "
                    f"answers, and run no maxpm wait. You end when {who} ends you. {r} go gives the list again."]
        else:
            out.append(f"Do not take other work in this session unless the person asks: then run {r} go --role worker.")
        print("\n".join(out).rstrip())
        return
    if b["role"] == "expert":
        x = b["expert"]
        out += [f"EXPERT on: {x['topic']}",
                f"  A person talks with you about this, for hours or days. MaximizePM never ends you as it ends a worker "
                f"(no wait END, no idle_end, no hand-over); you end when a person ends you, or after "
                f"{core.show_time(x['ends_at']) if x['ends_at'] else 'never'} with no talk (expert_idle_end).",
                f"  Your notes (read them now, and keep them current with your topic, what you learned, the "
                f"decisions of your talks, what is open, and the files and canvases that matter; write them after each "
                f"decision): {r} expert notes {me}   new version: {r} expert notes {me} --put <file>",
                f"  Your prompt cache (#5023): after a pause, the person's next message reads your whole context again "
                f"at the cache write price. {r} expert list shows warm (minutes left) or cold, and the cost. When the "
                f"person is in the middle of a question and your cache goes cold within a few minutes, say so in one "
                f"line. A cold expert with a large context gets replaced by a fresh one that reads only your notes "
                f"(maxpm expert fresh {me}).",
                "  Rules:",
                "  - Answer the person. Take no queue work by yourself: no maxpm wait, no go --role worker. Claim an "
                "item only when the person tells you to.",
                f"  - Work that you find: {r} add \"<title>\" (--found-during <id> when it comes from an item). "
                f"Work that should start now: ask the manager to launch it: {r} ask manager \"<what>\".",
                "  - Show a long answer as a canvas: maxpm canvas put <file> --name <name>.",
                f"  - Messages to you come into this session; read them with {r} inbox, and answer them.",
                "  - Stand at your prompt after each answer."]
        for h in b.get("holds") or ():
            if h.get("tier") == "expert":
                out.append(f"  Your task: #{h['id']} {h['title']} (expert tier). You keep it while the person talks with "
                           f"you; no lease runs out. When the person says the talk is done: {r} done {h['id']} --output "
                           f"\"<what was decided, where it is written>\".")
                if (h.get("context") or "").strip():
                    # The talk so far (#4969): the expert reads it, and what it names, before its first answer.
                    out.append("  Its context (the talk so far, when the manager handed it over: read it, and the "
                               "canvases, sources, and sessions it names, before your first answer):")
                    out += ["    " + ln for ln in h["context"].strip().splitlines()]
            else:
                out.append(f"  You hold #{h['id']} {h['title']} (the person asked you to): finish it, "
                           f"{r} done {h['id']} --output \"...\", or {r} release {h['id']}.")
        print("\n".join(out).rstrip())
        return
    notes = b.get("queue_instructions") or []
    if notes:
        out.append("FROM YOUR QUEUE (read and act on these first, in order):")
        for n in notes:
            out.append(f"  e{n['id']} {'STOP REQUEST' if n['kind'] == 'stop' else 'instruction'}"
                       + (f" from {n['added_by']}" if n["added_by"] else "") + f": {n['body']}")
        out += [f"  When you have acted on one: {r} queue remove {me} e<id>", ""]
    for u in b.get("unsynced") or []:
        out += _writeback_lines(u, r, u.get("tracker", "")) + ["  Do this before your item below.", ""]
    gb = b.get("goal")
    if gb:
        out.append(f"YOUR GOAL {gb['name']} [{gb['project']}]: {gb['outcome'] or '(no outcome written)'}"
                   + ("   (you took it now: nobody owned it)" if b.get("took_goal") else ""))
        if gb["done_when"]:
            out.append(f"  done when: {gb['done_when']}")
        out.append(f"  Its agent items are reserved for you while you own it; each maxpm command renews your claim for "
                   f"{gb['lease']} (goal_lease), and your leases on its items last that long too.")
        def st(o):
            return ("ready" if o["ready"] else o["status"].replace("_", " ")
                    + (f" by {o['assignee']}" if o["assignee"] else "")) + ("; human" if o["doer"] == "human" else "") + (
                "".join(f"; waits on a backlog item #{b}" for b in o.get("backlog_blockers") or ()))
        out.append(f"  items: {gb['items_done']} done, {len(gb['items_open'])} open"
                   + (": " + ", ".join(f"#{o['id']} {_cut(o['title'], 40)} ({st(o)})" for o in gb["items_open"][:6])
                      if gb["items_open"] else ""))
        if gb.get("items_backlog"):
            out.append(f"  in the backlog (nobody takes them now; goal done refuses while they are tagged): "
                       + ", ".join(f"#{o['id']} {_cut(o['title'], 40)}" for o in gb["items_backlog"][:6]))
        for x in gb["blockers_held"][:5]:
            to = (f"--goal {x['goal']}" if x["goal"] and x["goal_owner"] else f"--item {x['id']}")
            out.append(f"  held by another session: #{x['id']} {_cut(x['title'], 40)} ({x['assignee']}"
                       + (f"; goal {x['goal']}, " + (f"owner {x['goal_owner']}" if x["goal_owner"] else "no owner")
                          if x["goal"] else "") + ")"
                       + f"   ask: {r} ask \"...\" {to}   or offer help: {r} offer \"...\" --item {x['id']}")
        out.append(f"  You own this outcome: add the items it needs ({r} add \"<title>\" tags them with it), take them, "
                   f"and when done-when holds: {r} goal done {gb['name']} --result \"<one line>\"")
        if gb.get("parent_handoff"):  # a sub-goal: the parent's handoff first
            first, *rest = _handoff_lines(gb["parent_handoff"]["goal"], gb["parent_handoff"])
            out += [f"  parent goal {gb['parent_handoff']['goal']}: {first.strip()}"] + rest
        out += _handoff_lines(gb["name"], gb.get("handoff"), gb.get("handoff_due"))
        out.append(f"  Keep the handoff current after each item ({r} goal handoff {gb['name']} --file <path>): "
                   f"the next owner starts from it, and goal release and give refuse while it is older than your last item.")
        out.append("")
    if b["role"] == "stopped":
        st = b["stop"]
        out.append(f"STOP REQUESTED by {st['stop_by'] or 'someone'}: {st['stop_reason']}")
        if b["ended"]:
            out += ["You hold nothing now. MaximizePM released your goals and targets and gave your queued items back.",
                    "End this session now: stop, and tell the user it has ended and why (the reason above)."]
        else:
            out.append("You still hold: " + ", ".join(f"#{h['id']} {h['title']}" for h in b["stop_holds"]))
            out += ["1. Commit the work that is finished (tests first, as the project says).",
                    f"2. Finished: {r} done <id> --output \"<commit>\". Not finished: {r} release <id> --note "
                    "\"<what is done, what is left>\", or hand it on: maxpm give <id> --to <agent>.",
                    f"3. Run {r} go again: it ends the session. Take no new work."]
        print("\n".join(out).rstrip())
        return
    if b["role"] == "context":
        cx = b["context"]
        out += [f"YOU ARE THE CONTEXT SESSION OF GOAL {cx['goal']} [{cx['project']}]: {cx['outcome'] or '(no outcome)'}",
                "  Workers of the goal start as forks of this session: they read what you read now from the cache.",
                "  You edit no file and claim no item. Read only what the next items need, up to base_max "
                f"{cx['base_max']} tokens of context.", ""]
        if cx.get("parent_handoff"):  # a sub-goal: the parent's handoff first
            first, *rest = _handoff_lines(cx["parent_handoff"]["goal"], cx["parent_handoff"])
            out += [f"  parent goal {cx['parent_handoff']['goal']}: {first.strip()}"] + rest
        out += _handoff_lines(cx["goal"], cx["handoff"], indent="  ")
        out.append("  Its open agent items:")
        for x in cx["items"]:
            out.append(f"    #{x['id']} {x['title']} ({'ready' if x['ready'] else 'waits'})")
            for label in ("notes", "context"):
                if x[label]:
                    out.append(f"      {label}: {_cut(' '.join(x[label].split()), 300)}")
            if x["touches"]:
                out.append(f"      touches: {', '.join(x['touches'])}")
        if cx["more"]:
            out.append(f"    ... {cx['more']} more (maxpm goal show {cx['goal']})")
        out += ["", "Steps (for you, the context session; a fork of you gets its own briefing from its own maxpm go):",
                "  1. Read the files the items touch, the parts that matter. Do not change them.",
                f"  2. If the handoff is missing or old, write it: {r} goal handoff {cx['goal']} --file <path>",
                f"  3. Then, as your last command: {r} goal base {cx['goal']} --ready",
                "  4. End the session. Do not run maxpm go or wait again."]
        print("\n".join(out).rstrip())
        return
    it = b.get("item")
    if b.get("model_skipped"):
        out.append((f"Skipped for your model {b.get('model')} (min/max model limits; another session takes them): "
                    if not b.get("chat") else "Skipped in this chat (another session takes them): ")
                   + "; ".join(f"#{x['id']} {_cut(x['title'], 40)}: {x['why']}" for x in b["model_skipped"][:4]))
        out.append("")
    if b.get("chat"):
        out += _chat_lines(r, bool(it)) + [""]
    if it:
        out.append(f"YOUR ITEM #{it['id']}: {it['title']}")
        # The pages show the short title (#5086): the item has none yet, so this session writes it now.
        out.append(f"  short:   {it['short']}" if it.get("short") else
                   f"  short:   none yet. Write it now: {r} edit {it['id']} --short \"<about 6 plain words: no ids, "
                   f"no file names>\"")
        if it.get("found_during"):
            out.append(f"  (found during #{it['found_during']}; now it is the most important ready item)")
        if it["notes"]:
            out.append(f"  notes: {it['notes']}")
        out += _context_lines(it)
        out += _fault_lines(b.get("fault"), r)
        for h in b.get("handoffs") or []:  # the context of the item's goals, from their owners
            first, *rest = _handoff_lines(h["goal"], h, h.get("due"))
            out += [f"  goal {h['goal']}: {first.strip()}"] + rest
        for name in b.get("shared_goals") or []:
            out.append(f"  goal {name} is shared: it has no owner, and other agents work on its other items at the "
                       f"same time. Tag an item you add for it: {r} add \"<title>\" --goal {name}. When you finish, "
                       f"bring its handoff up to date: {r} goal handoff {name} --file <path>")
        if b.get("human_wait"):
            w = b["human_wait"]
            left = core._short(core.parse_iso(w["until"]) - core.now())
            out.append("  WAITING ON A PERSON: " + ", ".join(f"#{h['id']} {h['title']}" for h in w["on"])
                       + f". You wait {left} more (human_wait_max {b.get('human_wait_max')}); then MaximizePM releases"
                       + f" #{it['id']} and you take other work. Work on what does not need the answer meanwhile.")
        if it.get("refs"):
            out.append("  It comes from the tracker issue(s) above: mark them in progress there, if the tracker has that state.")
        if it.get("needs_check"):
            out += [f"  CHECK FIRST: a session held this item before and its lease ran out without done. Look at",
                    f"  {r} show {it['id']} (history), git log --grep '#{it['id']}', and the files it touches.",
                    f"  Already done: {r} check {it['id']} done --note \"<commits>\"   Partly: work on, and say so in --output."]
        if it["waits_on_detail"] and b["role"] != "reviewer" and not b.get("human_wait"):
            out.append("  waited on (all done): " + ", ".join(f"#{d['id']} {d['title']}" for d in it["waits_on_detail"]))
        if it["unblocks_detail"]:
            out.append("  unblocks: " + ", ".join(f"#{d['id']} {d['title']}" for d in it["unblocks_detail"]))
        if it.get("output"):
            out.append(f"  earlier output: {it['output']}")
        for an in b.get("annotations") or []:  # #5655
            out.append(f"  marked-up page a{an['id']} ({_cut(an['url'], 60)}): {an['open']} of {an['notes']} note(s) "
                       f"open: {r} browser annotation {an['id']}")
        for cv in b.get("canvases") or []:  # #4769
            out.append(f"  canvas {cv['name']} v{cv['version']}: "
                       + (f"{cv['open_comments']} open comment(s) wait for a change: {r} canvas comments {cv['name']} --open"
                          if cv["open_comments"] else f"no open comments ({r} canvas show {cv['name']})"))
        if b.get("related"):
            out.append(f"  RELATED (look first: some of it may be done or in progress; {r} show <id>):")
            out += _related_lines(b["related"], "  ")
        if b["role"] == "deployer":
            t = b["target"]
            if not t["description"]:
                out.append(f"  target {t['name']}: (no description: {r} target describe {t['name']} \"how it deploys\")")
            elif t["description"] != it.get("context"):
                out.append(f"  target {t['name']}: {t['description']}")
            out.append("  ships:")
            for d in b["ships"]:
                out.append(f"    #{d['id']} {d['title']} ({d['status']})")
            if t["checks"]:
                out.append(f"  after the deploy, maxpm serve runs its checks at once as the watch of the release: "
                           f"{', '.join(t['checks'])}; a fault opens its own item, found during this deploy")
            else:
                out.append("  no check watches the release: the target and its projects have none (maxpm ops list)")
            if b.get("hooks"):
                out.append("  " + _hooks_brief(t["name"], b["hooks"]))
            for n in b.get("next_deploy", []):
                out.append(f"  next deploy #{n['id']} is collecting: " + (", ".join(f"#{d['id']}" for d in n["waits_on_detail"]) or "nothing yet"))
            p = b.get("release") or {}
            if p.get("current"):
                out.append(f"  this release was cut {p['current']['text']}: it holds exactly the items above. A later "
                           f"ship request joins the next deploy item, which starts after this deploy is done"
                           + (f", and not before {p['cadence']} after this cut (release cadence)" if p.get("cadence") else "")
                           + f". You decide a release sooner, with a reason: {r} target release-now {t['name']} "
                             f"--reason \"<why>\"")
            out += ["",
                    "Deploy as the target description says, run the checks it names, then put the release id or",
                    f"deployed commit in the output: {r} done {it['id']} --output \"<release id, checks passed>\"",
                    "A step needs a person's go (an approval, a command the auto mode classifier refuses): ask in the",
                    f"queue before you stop at your prompt: {r} ask <person> \"<what to approve>\" --item {it['id']}",
                    "While that question is open, maxpm serve keeps your lease and the target and starts no other",
                    "deployer. The person answers in your terminal or with maxpm answer."]
        if b["role"] == "reviewer":
            out.append(f"  release {it.get('target')}: the deploy waits on this review. It covers:")
            if it.get("release_rev"):
                out.append(f"  pinned at {it['release_rev']}: review that commit; your review commands get it as MAXPM_REV")
            else:
                out.append("  no pin yet: review pass pins the commit the folder holds after your steps (or --rev)")
            for d in it["waits_on_detail"]:
                out.append(f"    #{d['id']} {d['title']} ({d['status']})   {r} show {d['id']}")
            groups = it.get("review_steps") or []
            out += ["",
                    "Review the release as a whole: read each item's output (commit ids) and the changes in its project folder."]
            if it.get("context"):
                out.append("Your review process: " + it["context"])
            elif not groups:
                out.append("No review steps or review_prompt are set: check correctness, tests, and security, and read the diffs.")
            if groups:
                out.append("Review steps, per project (do each; pass runs the [run] steps in the project folder, and they must exit 0):")
                for g in groups:
                    out.append(f"  {g['project']}:")
                    for st in g["steps"]:
                        out.append(f"    {st['pos']}. [{st['kind']}] {st['text']}   (step {st['id']})")
            if it.get("check"):
                out.append(f"Pass runs this check first, and it must exit 0: {it['check']}")
            conf = " --confirm all" if any(st["kind"] == "do" for g in groups for st in g["steps"]) else ""
            out += [f"  It is good:   {r} review pass {it['id']}{conf} --output \"<what you checked>\"",
                    f"  Problems:     {r} review fail {it['id']} \"<fix 1>\" \"<fix 2>\" --note \"<what you found>\" [--project <name>]",
                    "                (MaximizePM adds the fixes as items the review waits on; the review comes back after them)",
                    "                Add --ask to let the user approve the list first (one item in Needs you).",
                    f"  Findings that do not block the release: {r} add \"<title>\" --found-during {it['id']} --project <name>",
                    f"  A commit in the range you review is yours: {r} release {it['id']} --author \"<your commits>\"",
                    "                (do not review your own work; MaximizePM gives this review to another session)",
                    "",
                    f"Then continue:  {r} go" + ("   at once, in the same turn." if b.get("auto_continue") else "")]
        elif b.get("has_history") and not b.get("new_name"):
            out += [
                "",
                "Rules as before. Short form:",
                f"  what you do now:       {r} now \"<about 10 plain words>\"   at each new step",
                f"  needs something first: {r} add \"<title>\" --blocks {it['id']} --keep|--release",
                f"  needs an item that exists: {r} dep {it['id']} --on <its id> --release   (a note alone is no link)",
                f"  other work found:      {r} add \"<title>\" --found-during {it['id']}",
                f"  a step for the user:   {r} add \"...\" --doer human --context \"...\" --blocks {it['id']}",
                f"  a limit of the machine (disk, build stop, push freeze): keep the item, do the other parts, then "
                f"{r} inbox --wait",
                f"  a refused command:     a step for the user, with the exact command and the reason of the refusal",
                *_look_rule(b, it, r, short=True),
                f"  vague item: make a reasonable choice and say what you chose in --output.",
                "",
            ]
        else:
            out += [
                "",
                "Rules:",
                f"  - Do this item only. Read `{r} show {it['id']}` again if you need the links.",
                f"  - Say what you do, for your row on the Sessions tab: {r} now \"<about 10 plain words>\" when you",
                f"    start and at each new step (no ids, file names or commands).",
                f"  - The item is vague: make a reasonable choice and say what you chose in --output. Ask the user",
                f"    (a human item, below) only when a wrong guess would be costly.",
                f"  - It needs something first: {r} add \"<title>\" --blocks {it['id']} --keep   (small; you do it now)",
                f"    or ... --blocks {it['id']} --release   (large or better for someone else; then run go again).",
                f"  - It needs an item that is in the queue already: {r} dep {it['id']} --on <its id> --release   (add the link;",
                f"    after a release with only a note such as \"waits on #12\", go gives this item to the next session).",
                f"  - You find other work: {r} add \"<title>\" --found-during {it['id']}. Do not do it now.",
                f"  - Waiting on something outside the queue: {r} blocked {it['id']} --reason \"<what>\", release, run go again.",
                # The three rules of #1599 (research #1597: 30 stops and 11 stands at the prompt for limits of the
                # machine, 10 stands after a refused command, an item held 6.5 hours for the word "push").
                f"  - A limit of the machine (a full disk, a build stop, a push freeze, quiet time during a release gate) "
                f"does not block the item:",
                f"    do the parts that do not need the limited thing, commit, keep #{it['id']}, and wait for the "
                f"manager's word that it ended:",
                f"    {r} inbox --wait   (start it again when it ends with nothing). Take no other item that needs the "
                f"same thing.",
                f"  - A command is refused (the auto mode classifier, a permission prompt): do the parts you can. Then "
                f"it is a step for the user",
                # Not the permission rule: the classifier refused the item that named it and a later session (#1669).
                f"    (below), with the exact command and the reason of the refusal; the user runs it or allows it. "
                f"A release to",
                f"    production or a deletion of data: {r} ask <person> \"<what to approve>\" --item {it['id']}, then "
                f"commit, {r} release {it['id']} --note \"<what is left>\" and go:",
                f"    no session waits at its prompt for a yes; the answer starts a fresh session (question_wait_end).",
                *_look_rule(b, it, r),
                f"  - You need the user (a decision, an approval, an account or payment step): put it in the queue, not only in chat:",
                f"    {r} add \"<what to decide or do>\" --doer human --context \"<exactly what, where the material is>\" --blocks {it['id']} --release",
                *([f"    Commit what is finished, and say in the item's --context what is left. Then run go again: take other work, or",
                   f"    wait in maxpm wait. When the person answers, MaximizePM starts a fresh session for #{it['id']} with the answer in its notes."]
                  if b.get("fresh_sessions") else []),
                f"    With --keep you hold #{it['id']} and wait for the answer at most {b.get('human_wait_max', '30m')} (human_wait_max); "
                f"then MaximizePM releases it and you take other work.",
                f"    When you ask the user in chat, one decision at a time in this form: maxpm guide decisions",
                f"    That is what notifies them. A quick question instead: {r} send question --to "
                + ("|".join(b.get("humans") or []) or "<person>") + " \"...\" --item " + str(it["id"]),
                "",
            ]
        if b["role"] != "reviewer":
            out += [
                f"When finished:  {r} done {it['id']} --output \"<what changed, commit id>\"",
                f"Then continue:  {r} go" + ("   at once, in the same turn: do not stop to report between items."
                                             if b.get("auto_continue") else ""),
            ]
        if b.get("continue_mode") == "tree":
            out.append("Keep taking items until go gives you none or you need the user. go gives you only the tasks of "
                       "your tree (linked to what you did in this session, or of your goal); when none is ready it says "
                       "IDLE: then wait as it says, and other work goes to a new session.")
        elif b.get("auto_continue"):
            out.append("Keep taking items until go gives you none or you need the user; then report what you finished.")
    elif b["role"] == "owner":
        if b.get("goal_action") == "judge":
            out += ["YOUR GOAL HAS NO OPEN ITEMS. Check its done-when test yourself.",
                    f"  It holds:        {r} goal done {gb['name']} --result \"<what it achieved>\"",
                    f"  It does not:     add the next items ({r} add \"<title>\" --context \"...\"), then {r} go",
                    f"  Not yours to do: {r} goal release {gb['name']}"]
        else:
            out += ["YOUR GOAL WAITS ON OTHERS, and nothing else here is ready for you.",
                    "  Ask or offer help to the holders above, then run go again later.",
                    f"  Or hand it on: {r} goal give {gb['name']} --to <agent>   or: {r} goal release {gb['name']}"]
    elif b.get("chat"):
        out += ["NOTHING FOR THIS CHAT NOW.",
                "Tell the user what waits on them (below), and ask what they want to do: manage the agents and "
                "plan (maxpm manage), or answer what waits on them. Do not wait for work in a chat."]
    else:
        out.append("NOTHING FOR YOU NOW.")
        if b.get("nothing_planned"):
            out.append(f"  {b['why'][0].upper() + b['why'][1:]}. Tell the user; what they want done next becomes tasks "
                       f"(maxpm add; how: maxpm guide manager, section Writing tasks).")
        if b.get("queue_waiting"):
            out.append("Your queue holds items that are not ready yet: " + ", ".join(
                f"#{e['item']} {_cut(e['title'], 40)}" for e in b["queue_waiting"][:5]) + "; maxpm wait wakes you when one is.")
        for h in b.get("held_by_others", []):
            out.append(f"  #{h['id']} {h['title']}  (held by {h['assignee']})")
        for w in b.get("waits_on_backlog", [])[:5]:
            out.append(f"  #{w['id']} {_cut(w['title'], 50)}  (waits on a backlog item "
                       + ",".join(f"#{x}" for x in w["backlog"]) + "; a person or the manager decides: maxpm activate <id>)")
        if b.get("backlog"):
            out.append(f"  {b['backlog']} item(s) here are in the backlog: nobody takes them now (maxpm backlog).")
        how = ([
            "  Run it as a background command (Claude Code: run_in_background), then end your turn: a message that",
            "  a person or a session types to you reaches you at once. The harness wakes you when the wait prints.",
            "  After a talk, end your turn with the wait running again (start it only when the last one ended).",
        ] if b.get("wait_background") else [
            "  Run it in the foreground (give the shell command a 10-minute time limit), and never end your turn",
            "  at your prompt: a blocking wait costs no tokens, and MaximizePM ends an agent idle at its prompt.",
            "  MESSAGE: a person typed to you while it ran: answer the text, then run wait again.",
        ])
        out += [
            "",
            f"Wait for work: {r} wait",
            *how,
            "  It returns when work is pushed to you, an item gets ready here, or a message comes. Then do what it",
            "  prints: WORK: run go. No work yet: run wait again. This wait ended: do nothing for it.",
            "  END: no work came within wait_max; stop, tell the user this session has ended, and report",
            "  everything you finished in it.",
            f"  To work in another project instead: maxpm project list, then {r} go --project <name>.",
        ]
    msg = _unread_text(b.get("messages") or {"unread": 0, "questions": 0}, me)
    if msg:
        out.append("")
        out.append(f"Messages for you: {msg}. Read them before you start.")
    if b.get("human_waiting"):
        out.append("")
        out.append(f"Waiting on the user (tell them; if you can do one or work around it: {r} takeover <id> --note \"how\"):")
        for h in b["human_waiting"]:
            blocks = "; ".join(f"blocks #{d['id']} {d['title']} (P{d['priority']})" for d in h.get("blocks", [])[:2])
            out.append(f"  #{h['id']} {h['title']}" + (f"  ({blocks})" if blocks else ""))
    print("\n".join(out))


def _writeback_lines(it, r="maxpm", tracker=""):
    """What to do in the tracker after an item with links closes."""
    todo = [x for x in it["refs"] if not x.get("synced_at")]
    if not todo:
        return []
    verb = "close it" if it["status"] == "done" else "close it as not done"
    out = [f"UPDATE THE TRACKER" + (f" ({tracker})" if tracker else "") + f": #{it['id']} {it['title']} is {it['status']}."]
    for x in todo:
        out.append(f"  {x['ref']}" + (f"  {x['url']}" if x["url"] else "") + f": post the output as a comment and {verb}.")
    if it.get("output"):
        out.append(f"  output: {it['output']}")
    out.append(f"  Then: {r} synced {it['id']}")
    return out


def render_space(res):
    """maxpm cleanup --space / --dry-run (#5711): each size against its limit, and what goes (or went)."""
    from .space import show_size as sz
    d, f = res["db"], res["folder"]
    verb = "would remove" if res["dry_run"] else "removed"
    print(("DRY RUN: nothing is removed. " if res["dry_run"] else "") + "Size limits (db_max_size, folder_max_size):")
    ar = res.get("archive") or {}
    if ar.get("items"):
        print(f"  archive: {'would move' if res['dry_run'] else 'moved'} {ar['items']} items closed more than "
              f"{ar['after']} ago, with {ar['rows']} rows of their history, into the archive file (archive_after)")
    ad = res.get("archive_db")
    if ad:
        print(f"  archive file: {sz(ad['used'])}, limit {sz(ad['limit'])}"
              + (f" -> {sz(ad['used_after'])}" if ad["used_after"] != ad["used"] else ""))
        for r in ad["removed"]:
            print(f"    {verb} {r['rows']} rows of {r['what']}, {sz(r['bytes'])} (older than {r['older_than']})")
    for name, x, now_, after in (("database", d, d["used"], d["used_after"]), ("folder", f, f["size"], f["size_after"])):
        state = "off" if not x["limit"] else ("over" if now_ > x["limit"] else "under")
        line = f"  {name}: {sz(now_)}, limit {sz(x['limit'])} ({state})"
        if after != now_:
            line += f" -> {sz(after)}" + (" (estimate)" if res["dry_run"] else "")
        print(line + (f"  [{f['path']}]" if name == "folder" else ""))
        for r in x["removed"]:
            n = r.get("rows", r.get("files"))
            unit = "rows" if "rows" in r else "files"
            print(f"    {verb} {n} {unit} of {r['what']}, {sz(r['bytes'])}"
                  + (f" (older than {r['older_than']})" if r.get("older_than") else ""))
        if x.get("over") and x["limit"]:
            print(f"    still over: nothing older is removable; raise the limit (maxpm config set "
                  f"{'db' if name == 'database' else 'folder'}_max_size <size>) or lower a space_*_after age")
    k = f.get("kept")
    if k and k["browser_profiles"]:
        print(f"  kept: {k['browser_profiles']} browser profiles ({sz(k['bytes'])} without their caches; "
              f"{k['running']} running): sign-ins, never removed")
    if d.get("vacuum_error"):
        print(f"  the database file did not shrink (VACUUM: {d['vacuum_error']}); the next run tries again")


def render_cleanup(rows, r="maxpm"):
    if not rows:
        print("Nothing suspect: every open item looks as the queue says.")
        return
    print(f"{len(rows)} item(s) may be done or stale. Check each one: its history (maxpm show <id>), "
          f"git log --grep '#<id>', and the files it touches. Then record what you found.")
    for s in rows:
        print(f"#{s['id']:<4} [{s['project']}] {_cut(s['title'], 70)}" + ("  (person's item)" if s["doer"] == "human" else "")
              + f"  ({', '.join(s['reasons'])})")
        for e in s["evidence"][:4]:
            print(f"       {e}")
    if any(core.UNNEEDED in s["reasons"] for s in rows):
        print(f"A close may have made it unnecessary: {r} done <id> --note \"<why>\"  |  {r} drop <id> --note \"<why>\"  |  "
              f"{r} merge <keep> <id> --note \"<why>\"  |  still needed: {r} keep <id> --close <closed id> --note \"<why>\"")
    print(f"Record it: {r} check <id> done --note \"<commits or files>\"   |   partial --note \"<what is left>\"   |   open")


def render_wait(res):
    r = f"maxpm --as {res['agent']}"
    if res["result"] == "stop":
        st = res["stop"]
        print(f"STOP: {st['stop_by'] or 'someone'} asked this session to stop: {st['stop_reason']}. "
              + ("MaximizePM released your goals and targets. End this session now, and tell the user it has ended and why."
                 if res["ended"] else f"You still hold work: run {r} go, and follow it."))
    elif res["result"] == "work":
        print(f"WORK: {res['why']}. Run now: {r} go")
    elif res["result"] == "again":
        print(f"No work yet. Run again at once: {r} wait   (this session ends by itself in {res['left']} without work)")
    elif res["result"] == "left":
        print(f"This wait ended: {res['why']}. Nothing to do for it; continue what you do now.")
    elif res["result"] == "message":  # a foreground wait (#5256)
        print(f"MESSAGE: {res['why']}. Read the text and answer it now; then run again: {r} wait")
    else:
        print(f"END: no work came in {res['waited']}. MaximizePM "
              + ("kept your registration: " + res["kept"] if res.get("kept") else "released your goals and unregistered you")
              + ". Stop now, and tell the user this session has ended and its tab can be closed.")


def _secret_op(conn, args, actor, timeout=30):
    """Op "secret" (routes/secrets.py) here, or through maxpm serve when this command cannot reach the store (the
    Keychain from an agent's sandbox) or reads the project's browser, which runs in maxpm serve."""
    from . import vault
    if vault.needs_server() or args.get("from_browser") is not None:
        return ask_server(conn, "secret", args, actor=actor, timeout=timeout)
    from .routes import secrets
    return secrets.act(conn, args, actor)


def _secret_value(conn, name, actor, use):
    import base64
    return base64.b64decode(_secret_op(conn, {"action": "get", "name": name, "use": use}, actor)["value_b64"])


def _secret_cmd(conn, a, actor):
    """maxpm secret ... (#5757): no reply and no output of this command holds a value."""
    import base64
    import re
    import shlex
    import subprocess
    from . import vault
    cmd = a.secmd
    if cmd == "put":
        vault.check_name(a.name)
        project = a.project
        if not project:
            here = core.projects_for_dir(conn, os.getcwd())
            project = here[0] if len(here) == 1 else None
        args = {"action": "put", "name": a.name, "project": project, "note": a.note}
        if a.from_browser is not None:
            return _secret_op(conn, {**args, "from_browser": a.from_browser, "match": a.match, "within": a.within,
                                     "cwd": os.getcwd()}, actor, timeout=60)
        if sys.stdin.isatty():
            import getpass
            data, how = getpass.getpass(f"The value of {a.name} (it is not shown): ").encode(), "typed"
        else:
            data, how = sys.stdin.buffer.read(), "stdin"
        if not data:
            raise RiverError("no value came: a person types it at the prompt of maxpm secret put <name> in a terminal; "
                             "a session pipes it in (... | maxpm secret put <name>) or uses --from-browser")
        return _secret_op(conn, {**args, "value_b64": base64.b64encode(data).decode(), "how": how}, actor)
    if cmd == "pipe":
        argv = a.command[1:] if a.command[:1] == ["--"] else a.command
        if not argv:
            raise RiverError("name the command after --: maxpm secret pipe <name> -- gh secret set <NAME>")
        data = _secret_value(conn, a.name, actor, "pipe: " + shlex.join(argv))
        try:
            rc = subprocess.run(argv, input=data).returncode
        except OSError as e:
            raise RiverError(f"the command did not start: {e.strerror or e}")
        if rc:
            raise RiverError(f"{argv[0]} ended with exit {rc}; it read the secret {a.name} on its stdin")
        return {"secret": a.name, "piped_to": shlex.join(argv), "exit": rc}
    if cmd == "env":
        key = a.key or re.sub(r"[^A-Za-z0-9_]", "_", a.name).upper()
        if key[:1].isdigit():
            key = "_" + key
        data = _secret_value(conn, a.name, actor, f"env: {a.file} {key}")
        out = vault.set_env_line(a.file, key, data)
        return {"secret": a.name, "file": a.file, "key": key, **out}
    if cmd == "list":
        return _secret_op(conn, {"action": "list", "project": a.project}, actor)
    if cmd == "log":
        return _secret_op(conn, {"action": "log", "name": a.name, "limit": a.n}, actor)
    if cmd == "delete":
        return _secret_op(conn, {"action": "delete", "name": a.name}, actor)
    raise RiverError(f"secret: no command {cmd}")


def render_secret(a, res):
    cmd = a.secmd
    if cmd == "put":
        print(f"saved secret {res['secret']}" + (" (it replaced the old value)" if res["replaced"] else "")
              + f": {res['bytes']} bytes in the {res['store']} store. Give it to a command with "
              f"maxpm secret pipe {res['secret']} -- <command>")
    elif cmd == "pipe":
        print(f"gave secret {res['secret']} to {res['piped_to']} on its stdin (exit 0)")
    elif cmd == "env":
        print(f"{'replaced' if res['replaced'] else 'added'} {res['key']} in {res['file']} from secret {res['secret']}")
    elif cmd == "delete":
        print(f"deleted secret {res['secret']}")
    elif cmd == "list":
        rows = res["secrets"]
        if not rows:
            print(f"no secrets ({res['store']} store). Save one: maxpm secret put <name>")
        for r in rows:
            used = f"used {r['uses']}x, last {core.show_time(r['used_at'])} by {r['used_by'] or 'a person'}" \
                if r["uses"] else "never used"
            print(f"  {r['name']:<28} {r['project'] or '-':<14} saved {core.show_time(r['updated_at'])} by "
                  f"{r['updated_by'] or 'a person'}; {used}" + (f"  ({r['note']})" if r["note"] else ""))
    elif cmd == "log":
        for r in res["log"]:
            print(f"  {core.show_time(r['at'])}  {r['name']:<24} {r['action']:<8} {r['actor']}"
                  + (f"  #{r['item_id']}" if r["item_id"] else "") + (f"  {r['detail']}" if r["detail"] else ""))
        if not res["log"]:
            print("no puts or uses yet")


def _canvas_dispatch(conn, a, actor):
    """maxpm canvas ...: the canvas operations of core (#4648); MCP and HTTP call the same functions."""
    v = a.cvcmd
    if v == "put":
        if a.text is not None and a.file:
            raise RiverError("give the page as a file or --text, not both")
        if a.text is not None:
            body = a.text
        elif a.file == "-":
            body = sys.stdin.read()
        elif a.file:
            try:
                body = Path(a.file).expanduser().read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                raise RiverError(f"cannot read {a.file}: {getattr(e, 'strerror', None) or e}")
        else:
            raise RiverError("give the page: maxpm canvas put <file> --name <name> (or - for stdin, or --text)")
        pictures = (0, [])
        if a.file and a.file != "-":  # local pictures go inline, so the canvas shows them anywhere (#4983)
            try:
                kind = core.canvas_kind(a.kind, a.file)
            except RiverError:
                kind = None  # canvas_put says what is wrong, or takes the kind of the canvas that exists
            if kind in ("md", "html"):
                body, n, missing = core.inline_pictures(body, kind, Path(a.file).expanduser().resolve().parent)
                pictures = (n, missing)
        project = a.project
        if not project and a.item is None and not core._held_item(conn, actor):
            names = core.projects_for_dir(conn, os.getcwd())
            project = names[0] if len(names) == 1 else None
        res = core.canvas_put(conn, a.name, body, actor, a.kind, a.title, a.item, project, a.note,
                              a.file if a.file and a.file != "-" else None)
        return dict(res, pictures_inline=pictures[0], pictures_missing=pictures[1]) if any(pictures) else res
    if v == "kit":
        return core.canvas_kit()
    if v == "list":
        return core.canvas_list(conn, a.project, a.item, actor if a.mine else None, a.deleted)
    if v == "show":
        return core.canvas_show(conn, a.name, a.project, a.version)
    if v == "comments":
        return core.canvas_comments(conn, a.name, actor, a.open, a.project)
    if v == "comment":
        anchor = {"quote": a.quote, "path": a.path} if (a.quote or a.path) else None
        return core.canvas_comment(conn, a.name, a.text, actor, anchor, a.version, a.project, a.send)
    if v == "send":
        return core.canvas_send(conn, a.name, actor, a.comments or None, a.project)
    if v == "reply":
        return core.canvas_reply(conn, a.comment, a.text, actor)
    if v == "resolve":
        return core.canvas_resolve(conn, a.comment, actor, a.note)
    if v == "open":
        core.canvas_show(conn, a.name, a.project)  # a wrong name says so before maxpm serve is asked
        try:
            return ask_server(conn, "canvas_open", {"name": a.name, "project": a.project, "session": a.session},
                              actor=actor)
        except RiverError as e:
            raise RiverError(f"the page cannot open it now ({e}); the person sees the canvas in Sessions > Artifacts")
    if v == "delete":
        return core.canvas_delete(conn, a.name, actor, a.project)
    if v == "section":
        if sum(x is not None for x in (a.text, a.file, a.pending)) != 1:
            raise RiverError("give the section's text one way: a file, - for stdin, --text, or --pending")
        body = a.text
        if a.file == "-":
            body = sys.stdin.read()
        elif a.file:
            try:
                body = Path(a.file).expanduser().read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                raise RiverError(f"cannot read {a.file}: {getattr(e, 'strerror', None) or e}")
        return core.canvas_section(conn, a.name, a.heading, body, actor, a.project, a.note, a.pending)
    if v == "answer":
        return core.canvas_answer(conn, a.name, a.decision, a.text, actor, a.project)
    if v == "save":
        return core.canvas_save(conn, a.name, a.path, a.version, a.project, a.force)
    if v == "publish":
        return core.canvas_publish(conn, a.name, actor, a.version, a.project, a.yes)
    raise RiverError(f"unknown canvas command {v}")


def page_lines(p):
    """The realquicksite page of a canvas in a few lines (#5789): link, private or shared, end date, problems."""
    if not p:
        return []
    out = []
    if p.get("url"):
        ends = core.show_time(p["expires_at"]) if p.get("expires_at") else "?"
        how = "shared: anyone with the link opens it" if p.get("access") == "link" else "private"
        out.append(f"  realquicksite: {p['url']}  {how}  v{p.get('version')}  "
                   f"{'ended' if p.get('ended') else 'ends'} {ends}")
    if p.get("state") == "publishing":
        out.append("  realquicksite: publishing now")
    if p.get("error"):
        out.append(f"  realquicksite: {'the last publish failed: ' if p.get('state') == 'failed' else ''}{p['error']}")
    for x in (p.get("problems") or [])[:10]:
        out.append(f"  problem at the {x.get('viewport')} width ({x.get('kind')}): {x.get('message')}"
                   + (f" [{x['selector']}]" if x.get("selector") else ""))
    return out


def _canvas_line(d):
    counts = [f"{d['open_comments']} open comment(s)"] if d["open_comments"] else []
    counts += [f"{d['drafts']} draft(s)"] if d["drafts"] else []
    return (f"{d['name']}  v{d['version']} {d['kind']}  {d['project']}" + (f"  #{d['item_id']}" if d["item_id"] else "")
            + (f"  \"{d['title']}\"" if d["title"] else "") + f"  by {d['agent'] or '?'} {core.show_time(d['updated_at'])}"
            + (f"  [{', '.join(counts)}]" if counts else "") + ("  (deleted)" if d.get("deleted_at") else ""))


def _comment_lines(r, indent="  "):
    a = r.get("anchor")
    part = "the whole page"
    if a:
        part = (f"\"{a['quote']}\"" if a.get("quote") else f"\"{(a.get('text') or '')[:80]}\"") + \
               (f" ({a['path']})" if a.get("path") else "")
    out = [f"{indent}c{r['id']} [{r['state']}] {r['author']} on {part} (v{r['version']}): {r['body']}"]
    for x in r.get("replies", []):
        out.append(f"{indent}  reply c{x['id']} {x['author']}: {x['body']}")
    if r.get("resolve_note") or r["state"] == "resolved":
        out.append(f"{indent}  resolved by {r.get('resolved_by')}" + (f": {r['resolve_note']}" if r.get("resolve_note") else ""))
    return out



def render_annotations(a, res):
    """maxpm browser annotations and annotation <id> (#5655)."""
    from . import annotate
    if a.bcmd == "annotations":
        if not res["sets"]:
            print("no marked-up pages" + (" with open notes" if a.open else "")
                  + ": the person makes them with Annotate in the Browser pane")
        for s in res["sets"]:
            print(f"a{s['id']}  {s['created_at'][:16].replace('T', ' ')}  {len(s['notes'])} note(s), {s['open']} open"
                  f"  {_cut(s['url'], 60)}  from {s['author'] or 'the person'} to {s['agent'] or '(no session)'}"
                  + (f"  #{s['item_id']}" if s["item_id"] else ""))
        if res["sets"]:
            print("read one: maxpm browser annotation <id>")
        return
    s = res
    print(f"a{s['id']} from {s['author'] or 'the person'} to {s['agent'] or '(no session)'}, "
          f"{s['created_at'][:16].replace('T', ' ')}" + (f", item #{s['item_id']}" if s["item_id"] else ""))
    print(f"page: {s['url']}" + (f" ({s['title']})" if s["title"] else "") + f", {s['w']} × {s['h']} px")
    print(f"picture: {s['picture']}  (read it: the numbers in it are the notes')")
    for x in s["notes"]:
        st = {"open": "open", "fixed": "fixed", "not_fixed": "not fixed"}[x["state"]]
        print(f"{x['n']}. [{st}] {annotate.note_line(x)}")
        if x.get("fix"):
            print(f"   {x['fix']['by']}: {x['fix']['note']}")
    if s.get("drawings") and annotate._drawn(s["drawings"]):
        print(f"drawings in the picture: {annotate._drawn(s['drawings'])}")
    if s["open"]:
        print(f"after each fix: maxpm browser annotation {s['id']} fix <n> --note \"<what you changed>\" "
              f"(or not-fixed <n> --note \"<why>\")")


BROWSER_ACTS = {"open", "click", "type", "fill", "select", "check", "uncheck", "key", "back", "forward", "reload",
                "dialog", "upload", "hover", "scroll", "drag"}


def _snapshot_lines(s, title=True):
    """A snapshot as maxpm browser snapshot prints it (#5663)."""
    out = [f"page: {s.get('title') or '(no title)'}  {s.get('url') or ''}"] if title else []
    if s.get("delta"):
        out += s["lines"] or ["(no new or changed element)"]
        if s.get("removed"):
            out.append("gone: " + " ".join(s["removed"]))
    else:
        out += s["lines"] or ["(no interactive element in view of the snapshot)"]
    if s.get("more"):
        out.append(f"... {s['more']} more: --region <name> or --scope '<css>' for a part of the page, or --max <n>")
    if s.get("outside"):  # #5671: the default lists what shows in the window
        regs = "|".join(s.get("regions") or []) or "name"
        out.append(f"... {s['outside']} more out of the window: --all, --region <{regs}>, or maxpm browser find "
                   f"\"<word>\"")
    if s.get("frames"):
        out.append(f"({s['frames']} frame{'s' if s['frames'] > 1 else ''} not listed: open the frame's address)")
    if s.get("captcha"):  # #5688, policy g5
        out.append("This page shows a CAPTCHA test. Do not solve it: ask the person (maxpm ask <person> \"...\" --blocked)")
    return out


def _browser_result(action, res):
    """The short answer of one action (#5663): the page it left, and its snapshot when asked."""
    if action == "snapshot":
        return _snapshot_lines(res["snapshot"])
    if action == "text":
        return _text_lines(res)
    if action == "extract":  # #5677: one JSON array, one row on each line
        return ["[" + ",\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":"))
                                 for r in res.get("data") or []) + "]"]
    if action == "eval":
        return [json.dumps(res.get("value"), ensure_ascii=False)]
    what = []
    if "picked" in res:
        what.append("picked " + ", ".join(res["picked"]))
    if "checked" in res:
        what.append("checked" if res["checked"] else "not checked")
    if res.get("answered"):
        what.append(("accepted" if res["answered"]["accept"] else "dismissed") + f" the {res['answered']['type']} box")
    if res.get("uploaded"):  # #5679
        what.append("uploaded " + ", ".join(res["uploaded"]))
    if res.get("count", 1) > 1 or res.get("button"):  # #5669: the other clicks
        what.append(("double " if res.get("count", 1) > 1 else "") + (res.get("button") or "left") + " click")
    if res.get("scrolled"):
        s = res["scrolled"]
        what.append(f"scrolled to {s['y']} of {s['end']}" + (" (the end)" if s["y"] >= s["end"] else ""))
    head = ("new page: " if res.get("navigated") else "") + f"{res.get('title') or '(no title)'}  {res.get('url') or ''}"
    out = [head + (f"  ({'; '.join(what)})" if what else "")] + list(res.get("notes") or [])  # notes: #5668
    d = res.get("dialog")
    if d:  # #5674: the page waits for this box; every other command is refused until it is answered
        out.append(f"dialog {d['type']} {json.dumps(d['message'], ensure_ascii=False)}"
                   + (f" (default {json.dumps(d['default'], ensure_ascii=False)})" if d.get("default") else "")
                   + ": maxpm browser dialog " + ('accept "<text>"' if d["type"] == "prompt" else "accept")
                   + ", or dismiss")
    if res.get("outline"):  # #5670: the reply of open, and maxpm browser outline
        out += res["outline"]
    if res.get("changes"):  # #5673: what the action changed on the page, or 'no change'
        out += res["changes"]
    for d in res.get("downloads") or []:  # #5679: the files that the action downloaded
        size = f"{d['bytes'] / 1000:.1f} KB" if d["bytes"] >= 1000 else f"{d['bytes']} bytes"
        out.append(f"downloaded {d['path']} ({size})" if d["state"] == "completed" else
                   f"download {d['state']}: {d['name']} ({size} so far)" if d["state"] == "inProgress" else
                   f"download {d['state']}: {d['name']}")
    pe = res.get("page_errors")
    if pe:  # #5675: the page's errors since the action began
        n, r = pe.get("errors", 0), pe.get("requests", 0)
        out.append(", ".join(x for x in (f"{n} error{'' if n == 1 else 's'}" if n else "",
                                         f"{r} failed request{'' if r == 1 else 's'}" if r else "") if x)
                   + " (maxpm browser errors)")
    if "text" in res:  # #5672: open --text
        out += _text_lines(res)
    if res.get("snapshot"):
        out += _snapshot_lines(res["snapshot"], title=False)
    return out


def _text_lines(res):
    """The text of maxpm browser text (#5672), and a last line that says how much more there is and how to get it."""
    out = [f"({res['matches']} line{'' if res['matches'] == 1 else 's'} with the word)"] if "matches" in res and \
        res["matches"] is not None else []
    out.append(res.get("text") or "(no text)")
    if res.get("rest"):
        kb, sec = res["rest"] / 1000, res.get("section")
        go = (f"--section {json.dumps(sec, ensure_ascii=False)} " if sec else "") + f"--from {res['next']}"
        out.append(f"... {kb:.1f} KB more: {go}" + ("" if sec else ", --section \"<heading>\" (maxpm browser outline "
                                                                    "lists them), --grep <word>, or --all"))
    return out


FALLBACK_WORDS = {"not_found": "name not found", "ambiguous": "name ambiguous", "html_part": "html of a part",
                  "html_whole": "whole html", "picture": "element picture", "selector": "selector", "point": "point"}


def render_fallbacks(res):
    """maxpm browser fallbacks (#5692): the counts by site and kind, then the newest cases."""
    since = core.show_time(res["since"])
    if not res["sites"]:
        print(f"no fallbacks since {since}" + (f" on {res['site']}" if res["site"] else "")
              + ": the names worked on every page")
        return
    print(f"{res['total']} case{'' if res['total'] == 1 else 's'} since {since}, by site:")
    for s in res["sites"]:
        print(f"  {s['host']}  {s['n']}: " + ", ".join(f"{FALLBACK_WORDS.get(k, k)} {n}" for k, n in
                                                     sorted(s["kinds"].items(), key=lambda kv: -kv[1])))
    print("newest:")
    for c in res["cases"]:
        line = f"  {core.show_time(c['at'])}  {c['host']}{_cut(c['path'], 50)}  {FALLBACK_WORDS.get(c['kind'], c['kind'])}"
        if c["action"]:
            line += f" ({c['action']})"
        if c["tried"]:
            line += f"  tried {c['tried']}"
        if c["target"]:
            line += f"  with {c['target']}"
        if c["used"]:
            line += f"  used: {c['used']}"
        print(line + f"  {c['agent'] or ''}".rstrip())


def render_browser(a, res):
    b = a.bcmd
    if b in ("annotations", "annotation"):
        return render_annotations(a, res)
    if b == "fallbacks":
        return render_fallbacks(res)
    if b == "batch":
        for s in res["steps"]:
            st = s["step"]
            print("> " + " ".join(str(x) for x in [st["action"], st.get("selector") or st.get("url") or st.get("key")
                                                    or ""] if x))
            if "error" in s:
                print(f"maxpm: {s['error']}")
            else:
                print("\n".join(_browser_result(st["action"], s["result"])))
        if not res.get("ok"):  # the exit status says it too
            left = res.get("left") or 0
            raise RiverError(f"the batch stopped at line {len(res['steps'])}"
                             + (f"; the {left} after it did not run" if left else ""))
        return
    if b in ("snapshot", "find", "outline") or b in BROWSER_ACTS:
        print("\n".join(_browser_result("snapshot" if b == "find" else b, res)))
        return
    if b == "extract":  # #5677: JSON, one row on each line; the count when the limit cut it
        print("\n".join(_browser_result("extract", res)))
        if res.get("count", 0) > len(res.get("data") or []):
            print(f"maxpm: {len(res['data'])} of {res['count']} rows: --limit <n> for more", file=sys.stderr)
        return
    if b == "size":  # #5676
        f = res.get("forced")
        view = res.get("view")
        print(f"size {f['name']}" + (f" ({view[0]}x{view[1]} in the window)" if view else "") if f
              else "size: the side panel's" + (f" ({res['size'][0]}x{res['size'][1]})" if res.get("size") else
                                               " (none open: Chrome's own)"))
        return
    if b == "errors":  # #5675: one line each, with its first stack frame or its request (browser.error_line)
        print("\n".join(res.get("lines") or []) or "(no errors on this page)")
        return
    if b == "steps":  # #5680: the walk as lines for run (browser.step_words)
        print("\n".join(res.get("lines") or []) or "(no steps yet in this browser)")
        return
    if b == "record":  # #5682
        _record_lines(res)
        return
    if b == "record-steps":  # #5707
        if a.do == "start":
            print("Record is on: the person's clicks, keys and addresses in this browser become steps. "
                  "maxpm browser record-steps stop gives them as lines of maxpm browser run.")
        elif a.do == "stop":
            print(res.get("text") or "(no steps)")
        else:
            print(("Record is on. " if res.get("on") else "Record is off. Last recording: ")
                  + ("\n" + "\n".join(res["lines"]) if res.get("lines") else "no steps"))
        return
    if b == "run":  # #5680: the result (the last extract's rows), or the step that failed and the page
        if res.get("ok"):
            print(_browser_result("extract", {"data": res["result"]})[0] if res.get("result") is not None else
                  f"done: {res['done']} steps; {res.get('title') or '(no title)'}  {res.get('url') or ''}")
            return
        f = res["failed"]
        print(f"step {f['step']} ({f['line']}) failed after {res['done']} steps: {f['code']}: {f['message']}")
        print("\n".join(res.get("outline") or []))
        raise SystemExit(2)
    if b == "html" and "html" in res:
        print(res["html"])
        return
    if b == "image" and res.get("image"):  # #5688
        m = res["image"]
        size = f"{m['size'][0]}x{m['size'][1]}, " if m.get("size") else ""
        print(f"saved {m['path']} ({m['type']}, {size}{m['bytes'] / 1000:.1f} KB)")
        return
    if b == "text":
        print("\n".join(_text_lines(res)))
        return
    if b == "eval":
        print(json.dumps(res.get("value"), indent=2, ensure_ascii=False))
        return
    if res.get("file"):
        print(res["file"])
        return
    if not res.get("running"):
        print(f"browser of {res['project']}: not running (maxpm browser open <address> starts it)")
    else:
        print(f"browser of {res['project']}: {'a window' if res.get('window') else 'in the side panel (headless)'}")
        print(f"  page: {res.get('title') or '(no title)'}  {res.get('url') or ''}")
        if res.get("forced"):  # #5676
            print(f"  size {res['forced']['name']}, set by {res['forced'].get('by') or 'a session'} "
                  f"(maxpm browser size pane gives the side panel's size back)")
        if res.get("dialog"):
            print("  " + _browser_result("status", {"dialog": res["dialog"]})[1])
        if len(res.get("tabs") or []) > 1:
            for t in res["tabs"]:
                print(f"  {'*' if t['on'] else ' '} tab {t['id']}: {_cut(t['title'] or t['url'], 70)}")
    c = res.get("controller")
    if c:
        print(f"  controlled by {c['who']}" + (f" (subagent {c['sub']})" if c.get("sub") else "")
              + (" (the person)" if c["kind"] == "person" else "") + f" since {c.get('since', '')}")
    else:
        print("  controlled by nobody: maxpm browser control takes it")

def render_canvas(a, res):
    v = a.cvcmd
    if v == "kit":
        print(res["starter"].rstrip())
        print(f"<!-- {res['how']} -->")
        return
    if v == "section":
        print(("unchanged: " if res.get("unchanged") else "") + _canvas_line(res))
        if not res.get("unchanged"):
            print(f"  section '{res['section']}' {'added' if res['added'] else 'replaced'}: version {res['version']}")
        return
    if v == "answer":
        print(f"answered decision {res['decision']} ({res['name']}) as c{res['id']}; it went to the session that "
              f"writes the doc")
        return
    if v == "put":
        print(("unchanged: " if res.get("unchanged") else "") + _canvas_line(res))
        if not res.get("unchanged"):
            print(f"  version {res['version']} stored ({res['bytes']} bytes). People see it in the page's Artifacts; "
                  f"their comments come to you as a message: maxpm canvas comments {res['name']} --open")
        if res.get("pictures_inline"):
            print(f"  {res['pictures_inline']} local picture(s) put inline")
        for m in res.get("pictures_missing") or []:
            print(f"  picture not put inline: {m}")
        return
    if v == "list":
        if not res:
            print("(no canvases: maxpm canvas put <file> --name <name>)")
        for d in res:
            print(_canvas_line(d))
            if d.get("page") and d["page"].get("url"):
                print(page_lines(d["page"])[0])
        return
    if v == "show":
        if a.text:
            sys.stdout.write(res["shown"]["body"])
            return
        print(_canvas_line(res))
        sh = res["shown"]
        print(f"  shown: version {sh['version']} by {sh['author']} {core.show_time(sh['created_at'])}"
              + (f": {sh['note']}" if sh["note"] else ""))
        print("  versions: " + ", ".join(f"v{x['version']} {x['author']}" for x in res["versions"]))
        for line in page_lines(res.get("page")):
            print(line)
        for r in res["comments"]:
            print("\n".join(_comment_lines(r)))
        print("---- text of version %d (only the text: maxpm canvas show %s --text) ----" % (sh["version"], res["name"]))
        print(sh["body"])
        return
    if v == "comments":
        if not res["comments"]:
            print(f"(no {'open ' if a.open else ''}comments on canvas {res['canvas']})")
        for r in res["comments"]:
            print("\n".join(_comment_lines(r, "")))
        return
    if v == "comment":
        if res["state"] == "draft":
            print(f"comment c{res['id']} on canvas version {res['version']} is a draft; send your drafts: "
                  f"maxpm canvas send <canvas>")
        else:
            print(f"comment c{res['id']} sent")
        return
    if v == "send":
        print(f"sent {', '.join('c%d' % x for x in res['sent'])} on canvas {res['canvas']} to {res['to']} "
              f"(message #{res['message']})")
        return
    if v == "reply":
        print(f"reply c{res['id']} in the thread of c{res['parent_id']}")
        return
    if v == "resolve":
        print(f"comment c{res['id']} resolved")
        return
    if v == "open":
        who = f"the side panel of {res['session']}" if res["session"] else "the side panel"
        print(f"canvas {res['canvas']} opens in {who} on the page (the Sessions tab shows it at once when it shows "
              f"that session; else the page offers a Show button)")
        return
    if v == "delete":
        print(f"canvas {res['canvas']} deleted (a put of the same name brings it back)")
        return
    if v == "save":
        print(f"canvas {res['canvas']} version {res['version']} written to {res['path']}")
    if v == "publish":
        print(f"canvas {res['canvas']} version {res['version']} "
              + ("is on this page already" if res.get("already") else "published") + ":")
        print("\n".join(page_lines(res)))
        if res.get("access") != "link":
            print("  Only the person opens it (Google sign-in). They click Share on the page to send it; tell them "
                  "the link and the end date.")
        if res.get("problems"):
            print(f"  Fix the problems in the canvas, put a new version, and publish again: "
                  f"maxpm canvas publish {res['canvas']}")


def render_realquicksite(a, res):
    if a.rqcmd == "status":
        if res["connected"]:
            print(f"connected to realquicksite" + (f" as {res['email']}" if res.get("email") else "")
                  + (f" since {core.show_time(res['connected_at'])}" if res.get("connected_at") else "")
                  + f"; {res['pages']} canvas page(s). The key is in {res['file']} (mode 600).")
            if res.get("mode_fixed"):
                print(f"  the key file had mode {res['mode_fixed']}; it has 600 now")
        else:
            print("not connected to realquicksite. Connect: the button Share artifact in a canvas's bar, or "
                  "maxpm realquicksite connect")
        return
    if a.rqcmd == "disconnect":
        print(("disconnected: the key file is deleted. " if res["disconnected"] else "") + res["next"])
        return
    print(f"Open this address in your browser and click Allow: {res['url']}\n"
          f"realquicksite then comes back to maxpm serve, which keeps the key (it works for 30 minutes).")


# maxpm artifacts (#4838): the rows of the Artifacts tab in the terminal, so a manager can check what a worker made
# (a screenshot, a page, a canvas, a claude.ai artifact link) before it relays a 'Look at' item to the person.
ART_IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf")
ART_TEXT_MAX = 1_000_000


def _art_label(r):
    if r["kind"] == "file":
        return "image" if r["target"].lower().endswith(ART_IMAGES[:-1]) else "pdf" if r["target"].lower().endswith(".pdf") else "file"
    return {"artifact": "claude.ai", "page": "page", "rqs": "rqs page"}.get(r["kind"], r["kind"])


def artifact_rows(conn, a):
    """The rows for maxpm artifacts: the same function as the Artifacts tab (routes.artifacts.listing), each with its
    row number n and the page's id of it."""
    from . import notify, server
    from .routes import artifacts as art
    since = core._since(conn, a.since) if a.since else None
    if a.item is not None:
        core._item(conn, a.item)
    terminals = None
    if not a.agent and a.item is None:
        try:
            terminals = server.agent_terminals(conn)
        except Exception:  # no terminal list (tmux or Terminal not readable here): the agents seen lately only
            terminals = None
    rows = art.listing(conn, a.agent, a.item, since, terminals)
    return [dict(r, n=i + 1, id=notify.art_id(r["target"]), label=_art_label(r)) for i, r in enumerate(rows)]


def _item_artifacts(conn, it):
    """{count, item} for maxpm show (#4838): what the sessions that held the item made while they held it; for an
    item with none that was found during another one (a person's 'Look at' item), that one's. None: none."""
    import types
    for i in (it["id"], it.get("found_during")):
        if i is None:
            continue
        try:
            n = len(artifact_rows(conn, types.SimpleNamespace(agent=None, item=i, since=None)))
        except Exception:  # a transcript that cannot be read: no line
            n = 0
        if n:
            return {"count": n, "item": i}
    return None


def artifacts_cmd(conn, a):
    rows = artifact_rows(conn, a)
    if not a.show:
        return {"artifacts": rows[:a.limit], "total": len(rows)}
    if not a.ref:
        raise RiverError("name the row: maxpm artifacts show <n> (with the same --agent, --item, --since as the listing), "
                         "or its id or path")
    ref = a.ref.strip()
    row = None
    if ref.isdigit() and 1 <= int(ref) <= len(rows):
        row = rows[int(ref) - 1]
    row = row or next((r for r in rows if ref in (r["id"], r["target"])), None)
    if row is None:
        raise RiverError(f"no artifact {ref} in this listing ({len(rows)} rows); run maxpm artifacts with the same "
                         "--agent, --item, --since to see the row numbers")
    out = {"artifact": row}
    if row["kind"] == "canvas":
        c, v = core.canvas_version(conn, int(row["target"].split(":", 1)[1]))
        out.update(text=v["body"], read=f"maxpm canvas show {c['name']} (its versions and comments)")
    elif row["kind"] == "artifact":
        out["read"] = ("a claude.ai artifact: read it with the Artifact tool (action read, url " + row["target"]
                       + "); maxpm cannot fetch it")
    elif row["kind"] == "page":
        out["read"] = "a local page: open it with a browser tool, or curl it while its server runs"
    elif row["kind"] == "rqs":
        out["read"] = (f"the realquicksite page of canvas {row['name']} ({row.get('access') or 'private'}): read the "
                       f"canvas with maxpm canvas show {row['name']}; a private page opens only after the person's "
                       "Google sign-in")
    else:
        f = Path(row["target"]).expanduser()
        if not f.is_file():
            out["read"] = "the file is gone"
        elif f.suffix.lower() in ART_IMAGES:
            out["path"] = str(f)
            out["read"] = "a picture or a PDF: read the file at this path with your file-reading tool"
        else:
            data = f.read_bytes()
            out.update(path=str(f), text=data[:ART_TEXT_MAX].decode("utf-8", "replace"),
                       cut=len(data) > ART_TEXT_MAX)
    return out


def render_artifacts(res, a):
    if "artifact" in res:
        r = res["artifact"]
        print(f"{r['label']} {r['target']}  ({_art_where(r)})")
        if res.get("path"):
            print(f"path: {res['path']}")
        if res.get("read"):
            print(res["read"])
        if res.get("text") is not None:
            print()
            print(res["text"].rstrip("\n"))
            if res.get("cut"):
                print(f"... (cut at {ART_TEXT_MAX} bytes; read the file itself for the rest)")
        return
    rows = res["artifacts"]
    if not rows:
        print("No artifacts" + (f" of {a.agent}" if a.agent else "") + (f" for #{a.item}" if a.item is not None else "")
              + "." + ("" if a.agent or a.item is not None else " Without --agent or --item, the list holds the running "
                       "sessions and the ones seen in the last day (or since --since)."))
        return
    for r in rows:
        name = f' "{_cut(r["title"], 50)}" v{r["version"]}' if r["kind"] == "canvas" else ""
        extra = f", {r['open_comments']} open comment(s)" if r.get("open_comments") else ""
        print(f"{r['n']:>3}. {r['label']:<9} {r['target']}{name}  ({_art_where(r)}{extra}; id {r['id']})")
    if res["total"] > len(rows):
        print(f"... {res['total'] - len(rows)} more: -n {res['total']}")
    flags = "".join(f" --{k} {v}" for k, v in (("agent", a.agent), ("item", a.item), ("since", a.since)) if v is not None)
    print(f"read one: maxpm artifacts show <n>{flags}")


def _art_where(r):
    at = (r.get("at") or "")[:16].replace("T", " ")
    return ", ".join(x for x in (r.get("how"), r.get("session"), f"#{r['item']}" if r.get("item") else None,
                                 f"{at} UTC" if at else None) if x)


def _ops_state(c):
    s = c["state"]
    return f"paused until {c['paused_until']}" if s == "paused" and c.get("paused_until") else s


def _ops_run_line(r):
    took = f", {r['seconds']:.1f}s" if r.get("seconds") is not None else ""
    head = (f"{r['status']}" + (" (timed out)" if r.get("timed_out") else "") + took) if r.get("status") else "runs now"
    return f"run {r['started_at']}: {head}" + (f": {r['summary']}" if r.get("summary") else "") + \
        (f" | {r['perfdata']}" if r.get("perfdata") else "")


def render_ops_profile(a, res):
    if a.opcmd == "export":
        if res["out"]:
            n = len(res["profile"]["checks"])
            print(f"the profile of {res['profile']['scope']} ({n} check{'' if n == 1 else 's'}) is in {res['out']}")
        else:
            print(json.dumps(res["profile"], indent=2))
        return
    lines = ([f"  add {x['name']}: {x['summary']}" for x in res["add"]]
             + [f"  change {x['name']} ({', '.join(x['fields'])}): {x['summary']}" for x in res["change"]]
             + [f"  remove {n}" for n in res["remove"]]
             + [f"  set {k} = {v}" for k, v in res["settings"].items()])
    if not lines:
        print(f"the checks of {res['scope']} are as the profile says: nothing to change"
              + (f" ({len(res['same'])} the same)" if res["same"] else ""))
        return
    print(f"profile for {res['scope']}: " + ("written" if res["written"] else "the change it makes (nothing written yet)"))
    print("\n".join(lines))
    if res["same"]:
        print(f"  the same: {', '.join(res['same'])}")
    if not res["written"]:
        print(f"  write it: maxpm ops profile apply {a.file}" + (f" --scope {a.scope}" if a.scope else "") + " --yes")


def ops_outside_lines(res):
    """The lines of one pass of the watchdog, for its log: a line for each check that failed, changed its state,
    ran its repair or sent a notice."""
    out = []
    for r in res:
        if "error" in r or "skipped" in r:
            out.append(f"{core.iso(core.now())} {r['check']}: {r.get('error') or r.get('skipped')}")
            continue
        if r["status"] == "ok" and not (r.get("changes") or r.get("repair") or r.get("notice")):
            continue  # a pass that only passed writes no line: the log stays small
        line = f"{core.iso(core.now())} {r['check']}: {r['status']}"
        line += "".join(f"; {ch['to']}" for ch in r.get("changes") or [])
        if r.get("repair"):
            rp = r["repair"]
            line += f"; repair {rp['n']} " + ("ok" if rp["ok"] else "timed out" if rp["timed_out"] else
                                             f"exit {rp['exit_code']}")
        elif "repair_limit" in r:
            line += f"; no repair: {r['repair_limit']} ran in the window"
        if r.get("notice"):
            sent = r["notice"]["sent"]
            line += "; notice " + (", ".join(f"{x['channel']} {'sent' if x['ok'] else x['error']}" for x in sent)
                                   or "on no channel (notify_channels is empty)")
        out.append(line)
    return out


def ops_line_text(project, o):
    """The monitoring line of a project (core.ops_line): its checks by state, the open faults, the proposals."""
    if not o["checks"] and not o["proposed"]:
        return f"no checks (maxpm ops setup {project})"
    order = ("fault", "failing", "recovered", "new", "paused", "healthy")
    states = sorted(o["states"].items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order))
    text = f"{o['checks']} check{'' if o['checks'] == 1 else 's'}"
    if states:
        text += " (" + ", ".join(f"{n} {s}" for s, n in states) + ")"
    text += "; open faults: " + (", ".join(f"#{i}" for i in o["faults"]) or "none")
    if o["proposed"]:
        text += f"; {o['proposed']} proposed"
    return text + f" (maxpm ops list {project})"


def render_ops_setup(res):
    d = res["setup"]
    p = d.get("profile")
    if p:
        pscope = p["scope"].split(":", 1)[1]
        print(f"profile applied to {p['scope'].replace(':', ' ', 1)}: {len(p['add'])} added, {len(p['change'])} "
              f"changed, {len(p['remove'])} removed, {len(p['same'])} the same (maxpm ops list {pscope})")
        if p.get("outside"):
            print("  checks marked outside run in the watchdog: maxpm serve --install-watchdog (and --install-login, "
                  "so its repair can restart serve)")
    if not d["scope"]:
        return
    scope = d["scope"].split(":", 1)[1]
    print(f"ops setup of {d['scope'].replace(':', ' ', 1)}:")
    if not d["checks"] and d["asked"]:
        print("  no new check")
    for c in d["checks"]:
        if c["kept"]:
            print(f"  kept {c['name']}: {c['summary']}")
            if c["form"] == "beat":
                print(f"    the job reports each time it runs: maxpm ops beat {scope} {c['name']}")
        elif c.get("proposed"):
            print(f"  proposed {c['name']} to the manager: {c['summary']} (it runs once a person or the manager "
                  f"accepts it)")
        elif c.get("refused"):
            print(f"  not kept {c['name']}: {c['refused']}"
                  + ("" if d["asked"] else "; or propose it: add --why-propose \"<why it is needed>\""))
        else:
            print(f"  not kept {c['name']}: " + ("its first run failed (keep it anyway: --anyway)"
                                                if not d["asked"] else "you chose not to keep it"))
    n = d["notice"]
    if n:
        print("  test notice: sent to your phone" if n["sent"] else f"  test notice: not sent ({n['why']})")
    m = d["deadman"]
    if m:
        print(f"  outside switch: {m['url']}; the first beat "
              + ("arrived" if m["ok"] else f"failed ({m.get('error') or 'HTTP ' + str(m.get('status'))}); maxpm "
                 f"serve tries again each minute"))
    print(f"  the checks of {scope}: maxpm ops list {scope}")


def render_ops_proposal(a, res):
    p = res["proposal"]
    where = f"{p['scope']}/{p['name']}"
    if res["changed"] == "proposed":
        print(f"check {where} proposed" + (" (it replaces your earlier proposal)" if res["replaced"] else "")
              + f": {p['summary']}" + (f"; repair: {p['repair']}" if p["repair"] else ""))
        print("    it never runs as a proposal; the manager has a note. A person or the manager accepts it: "
              f"maxpm ops accept {p['scope']} {p['name']}")
    elif res["changed"] == "accepted":
        print(f"check {where}: the proposal of {p['proposed_by'] or 'a person'} accepted; "
              + ("added" if res["fields"] is None else f"changed ({', '.join(res['fields']) or 'nothing'})")
              + f": {p['summary']}. maxpm serve runs it from now on")
    else:
        print(f"check {where}: the proposal of {p['proposed_by'] or 'a person'} rejected ({p['note']})")


def visual_show_lines(res):
    """maxpm ops visual show: the last run and the three pictures of each shot."""
    out = [f"{res['name']}: {res['summary']}", f"  {res['url']}  at {res['at']}"]
    for shot, r in res["shots"].items():
        out.append(f"  {shot}: " + ("no approved picture yet" if r.get("diff") is None else f"{r['diff']:.2f}% differ"))
        out += [f"    approved: {r['baseline']}"] if r.get("baseline") else []
        out.append(f"    new:      {r['new']}")
        out += [f"    diff:     {r['diff_image']}  (the new one, faded; red: the pixels that differ)"] if r.get("diff_image") else []
    out.append(f"to approve the new pictures (a person or the manager): maxpm ops visual approve {res['name']} "
               f"[--shot <shot>] --note \"<why the page changed>\"")
    return out


def render_ops_system(a, res):
    if a.oscmd in ("edit", "accept", "reject"):
        what = {"accepted": "is in force", "proposed": "is proposed: a person or the manager accepts it (maxpm ops "
                "system accept " + res["project"] + ")", "rejected": "is rejected"}[res["state"]]
        print(f"system description of {res['project']}: "
              + (f"changed {', '.join(res['changed'])}; " if res.get("changed") else "") + f"the new version {what}")
        return
    shown = res["proposed"] if getattr(a, "proposed", False) else res["shown"]
    if getattr(a, "proposed", False) and not shown:
        print(f"{res['project']} has no proposed system description")
        return
    if not shown:
        print(f"{res['project']} has no system description yet. Write it: maxpm ops system edit {res['project']} "
              f"--set code.test=\"<the test command>\" ...   (maxpm ops system edit --help lists the fields)")
        return
    print(f"{res['project']}: system description, version {shown['version']} ({shown['state']}, by {shown['written_by'] or 'a person'} "
          f"{shown['written_at']})" + (f"; why: {shown['why']}" if shown.get("why") else ""))
    names = {"code": "The code", "infra": "The infrastructure", "rules": "The project's own rules"}
    for part, fields in res["fields"].items():
        print(f"\n{names[part]}")
        for f, help_ in fields.items():
            v = (shown["body"].get(part) or {}).get(f)
            print(f"  {part}.{f}: " + (v.replace("\n", "\n    ") if v else f"(none: {help_})"))
    if res["proposed"] and not getattr(a, "proposed", False):
        p = res["proposed"]
        print(f"\nA change by {p['written_by']} is proposed ({p['written_at']}; why: {p['why']}): maxpm ops system "
              f"show {res['project']} --proposed")


def _noise_line(r):
    return (f"n{r['id']} {r['scope']}/{r['check_name']}" + (f" subject {r['subject']}" if r["subject"] else "")
            + (f" matching {r['pattern']!r}" if r["pattern"] else "") + f": {r['reason']} ({r['state']}"
            + (f" until {core.show_time(r['until'])}" if r["state"] == "in force" else "")
            + (f"; {r['hits']} fault(s) kept out" if r["hits"] else "") + (f"; fault #{r['fault_id']}" if r["fault_id"] else "")
            + f"; by {r['created_by'] or 'a person'})")


def _schedule_line(x):
    zone = x.get("zone") or ""
    nxt = (f"next due {core.show_time(x['next_due'], zone)}" + (
        f", its item comes {core.show_time(x['next_add'], zone)}" if x["next_add"] != x["next_due"] else "")
           if x["next_due"] else "no due time within 5 years")
    return (f"s{x['id']} {x['project']}: {x['title']}  [cron '{x['cron']}', lead {x['lead']}, doer {x['doer']}, "
            f"P{x['priority']}]; {nxt}" + (f"; last item #{x['last_item']}" if x.get("last_item") else ""))


def render_schedule(a, res):
    if a.schcmd == "list":
        if not res["schedules"]:
            print("no schedules: maxpm schedule add <project> \"<title>\" --yearly MM-DD --lead 30d")
        for x in res["schedules"]:
            print(_schedule_line(x))
        return
    print(f"schedule {res['changed']}: {_schedule_line(res['schedule'])}")
    if res["changed"] == "added":
        print("maxpm serve adds each item at its time; maxpm schedule list shows them")


def render_ops(a, res):
    if a.ocmd == "record":
        if not res["rows"]:
            print("no detections" + (f" of kind {res['kind']}" if res["kind"] else "") +
                  (f" in the last {res['since']}" if res["since"] else "") + (f" of {res['project']}" if res["project"] else ""))
        for r in res["rows"]:
            sub = f":{r['subject']}" if r["subject"] else ""
            item = f"  #{r['item_id']}" if r["item_id"] else "  no item"
            print(f"{core.show_time(r['last_seen'])}  {r['kind'] or '?'}  {r['scope']}/{r['check_name']}{sub}  "
                  f"{r['status']}, {r['count']}x since {core.show_time(r['first_seen'])}{item}")
            print(f"    {r['summary'] or ''}")
        if res["kinds"]:
            print("kinds: " + ", ".join(f"{k} {n}" for k, n in sorted(res["kinds"].items(), key=lambda kv: -kv[1])))
        return
    if a.ocmd == "noise":
        if a.oncmd == "list":
            for r in res["rules"]:
                print(_noise_line(r))
            if not res["rules"]:
                print("no noise rules" + ("" if a.all else " in force (--all shows ended ones)"))
            return
        print(f"noise rule {res['changed']}: {_noise_line(res['rule'])}")
        return
    if a.ocmd == "system":
        return render_ops_system(a, res)
    if a.ocmd == "profile":
        return render_ops_profile(a, res)
    if a.ocmd == "setup":
        return render_ops_setup(res)
    if a.ocmd == "visual":
        print(f"{res['name']}: approved the new pictures of {', '.join(res['approved'])} ({res['folder']})")
        return
    if a.ocmd in ("propose", "accept", "reject"):
        return render_ops_proposal(a, res)
    if a.ocmd == "run":
        print(f"check {res['check']}: run {res['run']} started" + (" and read (a beat check runs no process)"
              if res["form"] == "beat" else " in maxpm serve")
              + f". The result: maxpm ops history {res['check'].replace('/', ' ', 1)}")
        return
    if a.ocmd == "deps":
        if not res["dependencies"]:
            print(f"{res['project']} names no dependency: maxpm ops system edit {res['project']} --set "
                  f"infra.dependencies=\"<vendor>: <status page> <addresses>\" (one a line)")
        for d in res["dependencies"]:
            print(f"{d['name']}: status {d['status'] or 'none'}; named by {', '.join(d['names'])}; check "
                  + (d["check"] + (" (added)" if d["name"] in res["added"] else "") if d["check"] else
                     "none" + ("" if d["status"] else " (no status page to check)")))
        if not res["added"] and any(not d["check"] and d["status"] for d in res["dependencies"]):
            print(f"add their checks: maxpm ops deps {res['project']} --add")
        return
    if a.ocmd == "cause":
        if res["passed"]:
            print(f"cause c{res['cause']} of #{res['item']} proven: the proof gave {res['result']} (it expects "
                  f"{res['expect']}). Now fix it or add its fix items (maxpm add \"<fix>\" --found-during {res['item']}); "
                  + (f"after the action, {res['check']} must pass, or #{res['item']} opens again" if res["check"]
                     else "the fault has no check to confirm the action"))
        else:
            print(f"cause c{res['cause']} of #{res['item']} NOT proven: the proof gave {res['result']}, and it expects "
                  f"{res['expect']}. Investigate again; fix items wait until a proof passes")
        return
    if a.ocmd == "kinds":
        k = res["kinds"]
        print("The rules, in order (the first that matches gives the kind):")
        for r in k["rules"]:
            print(f"  {r}")
        print("The kinds (maxpm ops kind <item> <kind> --why \"...\"):")
        for kid, d in k["kinds"].items():
            print(f"  {kid}: {d['signs']}. What happens: {d['action']}. Never: {d['never']}.")
        return
    if a.ocmd == "kind":
        print(f"#{res['item']} is of the kind {res['kind']}"
              + (f"; it waits on the person's step #{res['person']}" if res.get("person") else "")
              + ("; you released it, so the right session takes it next" if res["released"] else "")
              + (f". Known message {res['known']['line']!r}: {res['known']['state']}" if res.get("known") else ""))
        return
    if a.ocmd == "repair":
        print(f"repair of {res['check']} started in maxpm serve: {res['command']}. Its result goes on "
              + (f"#{res['item']}" if res.get("item") else "the check's history")
              + f", then the check runs once: maxpm ops history {res['check'].replace('/', ' ', 1)}")
        return
    if a.ocmd == "beat":
        print(f"beat of {res['check']} at {res['beat']} (it is a fault after no beat for {res['period']})")
        return
    if a.ocmd == "history":
        print(f"check {res['check']}  [{res['state']}]  key {res['key']}")
        print("runs, newest first:" if res["runs"] else "no runs yet: maxpm serve runs it when it is due")
        for r in res["runs"]:
            print(f"    {_ops_run_line(r)}")
            if r.get("confirms"):
                print(f"        it confirms the new fault of run {r['confirms']}, at once")
            if r.get("missed"):
                m = json.loads(r["missed"])
                if m.get("release"):
                    print(f"        late: the watch time {m['time']} of release #{m['release']}; {m['count']} time(s) "
                          f"passed since {core.show_time(m['since'])} with no run; it ran once")
                else:
                    print(f"        after a stop: {m['count']} times were due between {core.show_time(m['since'])} "
                          f"and {core.show_time(m['until'])}; it ran once")
            if r.get("subjects"):
                subj = json.loads(r["subjects"])
                if subj:
                    print("        subjects: " + ", ".join(f"{k} {v.get('status', '?')}" for k, v in sorted(subj.items())))
        if res["events"]:
            print("changes of state, newest first:")
        for e in res["events"]:
            print(f"    {e['at']} {e['key']}: {e['state']}" + (f" ({e['note']})" if e["note"] else ""))
        return
    if a.ocmd == "list":
        if not res["checks"]:
            print(f"no checks{' of ' + res['scope'] if res['scope'] else ''}. Add one: maxpm ops add "
                  f"<project|target> <name> <{'|'.join(res['forms'])}> ..., or start with: maxpm ops setup")
        for c in res["checks"]:
            last = c["last"]
            ran = f"last run {last['started_at']}: {last['status'] or 'runs now'}" if last else "never ran"
            print(f"{c['scope']}/{c['name']}  [{_ops_state(c)}; {ran}]")
            print(f"    {c['summary']}")
        if res.get("proposals"):
            print("proposed, not running (a person or the manager: maxpm ops accept|reject <scope> <name>):")
        for p in res.get("proposals") or []:
            what = f"change of {', '.join(p['changes'])}" if p["changes"] is not None else "new"
            print(f"  {p['scope']}/{p['name']}  [{what}; by {p['proposed_by'] or 'a person'} {p['proposed_at']}]")
            print(f"    {p['summary']}" + (f"; repair: {p['repair']}" if p["repair"] else ""))
            print(f"    why: {p['why']}")
        return
    c = res if a.ocmd == "show" else res["check"]
    if a.ocmd != "show":
        if res["changed"] is None:
            print(f"check {c['scope']}/{c['name']}: nothing changed")
        else:
            print(f"check {c['scope']}/{c['name']} {res['changed']}"
                  + (f" ({', '.join(res['fields'])})" if res.get("fields") else ""))
        if res["changed"] == "removed":
            print("    its runs and changes of state stay in the history")
            return
    print(f"{c['scope_kind']} {c['scope']}, check {c['name']}  [{_ops_state(c)}]  key {c['key']}")
    print(f"    {core.OPS_FORMS[c['form']]}")
    for k, v in c["config"].items():
        print(f"    {k}: {v}")
    print(f"    {core.ops_when(c)}, timeout {c['timeout']}, fault after {c['fail_after']} failed runs, healed after "
          f"{c['recover_after']} passed runs; severity {c['severity']}"
          + ("; a fault for each subject in the output" if c["subject"] else ""))
    if c["runbook"]:
        print(f"    runbook: {c['runbook']}")
    if c["repair"]:
        print(f"    repair: {c['repair']}")
    if c["runs_command"] or c["repair"]:
        print("    it runs a command outside the sandbox: only a person or the manager changes it"
              + (" (the repair on a deploy target: a person)" if c["repair"] and c["scope_kind"] == "target" else ""))
    for r in c.get("runs") or []:
        print(f"    {_ops_run_line(r)}")
    for e in c.get("events") or []:
        print(f"    {e['at']} {e['state']}" + (f": {e['note']}" if e["note"] and e["note"] != e["state"] else ""))


def render(a, res):
    c = a.cmd
    if c == "schedule":
        return render_schedule(a, res)
    if c == "ops":
        return render_ops(a, res)
    if c == "wait":
        return render_wait(res)
    if c == "usage":
        return render_usage(res, a.limit)
    if c == "health":
        return render_health(res)
    if c == "change":
        if a.chcmd == "list":
            if not res:
                print("(no changes; a person or the manager adds one: maxpm change add \"<what changed>\" --at <time>)")
            for r in res:
                print(f"  c{r['id']:<4} {r['at'][:16]}  {r['name']}" + (f"  ({r['note']})" if r["note"] else "")
                      + f"  [{r['actor'] or '?'}]")
        else:
            print(f"{'removed' if a.chcmd == 'remove' else 'added'} c{res['id']}: {res['name']} at {res['at'][:16]}")
        return
    if c == "show" and isinstance(res, dict) and res.get("archived"):
        print(f"ARCHIVED: #{res['id']} is in the archive of old items; a command that changes it brings it back")
    if c == "show" and a.brief:
        return _print_brief(res)
    if c == "cleanup" and isinstance(res, dict) and "folder" in res:
        return render_space(res)
    if c == "cleanup":
        return render_cleanup(res, f"maxpm --as {a.actor}" if a.actor else "maxpm")
    if c == "done" and isinstance(res, dict) and res.get("refs"):
        _print_show(res)
        lines = _writeback_lines(res, f"maxpm --as {a.actor}" if a.actor else "maxpm", res.get("tracker", ""))
        if lines:
            print()
            print("\n".join(lines))
        return
    if c == "keep" and isinstance(res, dict) and "kept" in res:
        print(f"kept after the close of #{res['closed']}: " + ", ".join(f"#{k['id']} {_cut(k['title'], 50)}" for k in res["kept"]))
        return
    if c == "release" and isinstance(res, dict) and res.get("hint"):
        _print_show(res)
        print()
        print("HINT: " + res["hint"])
        return
    if c == "go":
        return render_go(res)
    if c == "artifacts":
        return render_artifacts(res, a)
    if c == "realquicksite":
        return render_realquicksite(a, res)
    if c == "search":
        return render_search(res, a.limit)
    if c == "duplicates":
        return render_duplicates(res["duplicates"])
    if c == "merge":
        m = res["merged"]
        print(f"#{m['id']} {m['title']}: dropped as a duplicate of #{res['id']}"
              + (f"; moved to #{res['id']}: {', '.join(m['moved'])}" if m["moved"] else "") + ".")
        if m["skipped"]:
            print("  not moved (it would make a loop): " + ", ".join(m["skipped"]))
        _print_show(res)
        return
    if c == "plan":
        print(PLAN_GONE)
        return
    if c == "manage":
        return render_manage(res)
    if c == "add" and isinstance(res, dict) and "dry_run" in res:
        rows = res["items"]
        print(("Would add" if res["dry_run"] else "Added") + f" {len(rows)} item(s) to {res['project']}:")
        ref = lambda r: f"#{r['id']}" if "id" in r else f"line {r['line']}"
        for i, r in enumerate(rows):
            steps = [ref(x) for x in rows if x["parent"] == i]
            print(f"  {ref(r):<8} P{r['priority']} {r['doer']:<5} {r['title']}"
                  + (f"  waits on {', '.join(steps)}" if steps else ""))
        if res["dry_run"]:
            print("Run it again without --dry-run to add them.")
        return
    if c == "browser":
        return render_browser(a, res)
    if c == "canvas":
        return render_canvas(a, res)
    if c == "secret":
        return render_secret(a, res)
    if c == "goal" and a.gcmd == "handoff":
        return render_handoff(res, a)
    if c == "goal" and a.gcmd == "base":
        b = res["base"]
        if res["cleared"] or not b:
            print(f"goal {res['goal']}: no base" + ("" if res["cleared"] else
                  " (maxpm serve starts a context session for it when goal_context is on)"))
            return
        print(f"goal {res['goal']}: base session {b['session_id']} ({b['model'] or 'default model'}, "
              f"{b['tokens'] or '?'} tokens, {b['forks']} fork(s)), ready {b['ready_at']}, "
              + (f"warm until {b['warm_until']}" if b["warm"] else "cold: new workers start fresh"))
        if res.get("warning"):
            print(f"  {res['warning']}")
        if res["ready"]:
            print("  Your work as the context session is done: end this session now.")
        return
    if c == "goal":
        rows = res if isinstance(res, list) else [res]
        if not rows:
            print("(no open goals: maxpm goal add <project> <name> --outcome \"...\" --done-when \"...\")")
        for g in rows:
            print(_fmt_goal(g))
            if isinstance(res, dict):
                if g["done_when"]:
                    print(f"  done when: {g['done_when']}")
                if g["result"]:
                    print(f"  result: {g['result']}")
                if g.get("parent_handoff"):
                    first, *rest = _handoff_lines(g["parent"], g["parent_handoff"])
                    print("\n".join([f"  parent goal {g['parent']}: {first.strip()}"] + rest))
                print("\n".join(_handoff_lines(g["name"], g.get("handoff") and dict(
                    g["handoff"], text=g.get("handoff_text")), g.get("handoff_due"))))
                for it in g.get("items", []):
                    print("  " + _fmt_item(it, show_reason=False))
                if not g.get("items"):
                    print(f"  no items yet: maxpm add \"<title>\" --goal {g['name']}")
        if isinstance(res, dict) and res.get("goal_checks"):
            print("\n".join(_goal_check_lines(res["goal_checks"])))
        return
    if c == "policy":
        if a.polcmd in ("show", "add", "edit"):
            print(("added " if a.polcmd == "add" else "changed " if a.polcmd == "edit" else "")
                  + "\n".join(policy_full(res)))
            if a.polcmd == "add":
                print(f"  Name it in your item's output (maxpm done <id> --output \"... policy {res['pid']}\").")
        elif a.polcmd == "retire":
            print(f"retired {res['pid']} ({res['scope']}): {res['text']}")
        else:
            if not res:
                print("(no policies: maxpm policy add \"<rule>\" --reason \"<why>\" --source \"<who, date>\" "
                      "[--project P | --folder F] [--role R] [--adds-to g<n>])")
            for p in res:
                print(policy_line(p))
        return
    if c == "review" and a.rcmd == "step":
        rows = [res] if isinstance(res, dict) else res
        if not rows:
            where = f" for {a.project}" if getattr(a, "project", None) and a.scmd == "list" else ""
            print(f"(no review steps{where}: maxpm review step add <project> \"<instruction>\"  or  --run \"<command>\")")
        last = None
        for r in rows:
            if r["project"] != last:
                print(f"{r['project']}:")
                last = r["project"]
            print(f"  {r['pos']}. [{r['kind']}] {r['text']}   (step {r['id']})")
        return
    if c == "review" and a.rcmd == "pass" and isinstance(res, dict) and res.get("review_steps"):
        for r in res["review_steps"]:
            print(f"  step {r['id']} ({r['project']}): {r['result']}   {r['text']}")
    if c == "project" and a.pcmd == "tracker":
        print(f"{res['name']}: tracker " + (res["tracker"] or "none"))
        return
    if c == "project" and a.pcmd == "rename":
        print(f"project {res['was']} is now {res['name']}: {res['items']} items, {res['goals']} goals, the folder, "
              f"the rank, the target and the settings follow")
        print(f"  the old name {res['was']} does not work from now on; agents keep their names, and a new agent "
                  "gets the new name")
        return
    if c == "project" and a.pcmd in ("archive", "restore", "unarchive"):
        n = f"{res['open_items']} open item{'s' * (res['open_items'] != 1)}" + (f" ({res['in_progress']} in progress: {', '.join(res['holders'])})"
                                                 if res["in_progress"] else "")
        if res["archived"]:
            print(f"project {res['name']} is archived: its {n} left the queue and the lists; nothing is deleted")
            if res["in_progress"]:
                print("  the sessions that hold its items keep them until they finish or release them")
            print(f"  to bring it back: maxpm project restore {res['name']}")
        else:
            print(f"project {res['name']} is back, at rank {res['rank']}: its {n} in the queue again")
            print(f"  to move it up: maxpm project rank {res['name']} <rank>")
        return
    if c == "project" and isinstance(res, dict) and "archived_projects" in res:
        for p in res["archived_projects"]:
            print(f"{p['name']}  ({p['open_items']} open, {p['done_items']} done"
                  + (f"; archived {p['archived_at'][:10]}" if p["archived_at"] else "") + ")")
        print("(no archived projects)" if not res["archived_projects"] else "to bring one back: maxpm project restore <name>")
        return
    if c == "project":
        if isinstance(res, dict) and "ready_count" in res:
            print(f"{res['name']} (rank {res['rank']})")
            print("  " + (res["description"] or "(no description: maxpm project describe " + res["name"] + " \"...\")"))
            print("  target: " + (res.get("target") or "none (maxpm project target " + res["name"] + " <target>)"))
            g = res.get("release_gate")
            if g:
                print("  release gate: " + ({"person": f"a person's yes for each release (set on {g['project']})",
                                             "review": f"ships when its review passes (set on {g['project']})"}
                                            .get(g["approval"]) or f"not set: maxpm config set release_approval "
                                                                   f"review|person --project {res['name']}")
                      + "; cadence: " + (g["cadence"] or "off")
                      + (f" ({g['cadence_reason']})" if g["cadence_reason"] else
                         f" (why: maxpm target cadence {res['target']} <time>|off --reason \"...\")"))
            print("  tracker: " + (res.get("tracker") or "none (maxpm project tracker " + res["name"] + " \"<tracker> <where> via <tool>\")"))
            if res.get("ops") is not None:
                print("  monitoring: " + ops_line_text(res["name"], res["ops"]))
            if res.get("look"):
                print("\n".join("  " + line for line in look_setup_lines(res["name"], res["look"])))
            print("  items: " + ", ".join(f"{v} {'in backlog' if k == 'backlog' else k.replace('_', ' ')}" for k, v in res["counts"].items() if v))
            print("  working now: " + (", ".join(res["working_now"]) or "nobody"))
            if res["worked_recently"]:
                print("  worked here recently: " + ", ".join(res["worked_recently"]))
            for g in res.get("goals", []):
                print("  goal " + _fmt_goal(g))
            print(f"  ready ({res['ready_count']}):")
            for it in res["ready"]:
                print("    " + _fmt_item(it, show_reason=False))
            return
        rows = res if isinstance(res, list) else [res]
        for p in rows:
            print(f"{p['rank']:>2}. {p['name']}" + (f"  ({p['open_items']} open" + (
                f", {p['backlog_items']} in backlog" if p.get("backlog_items") else "") + ")" if "open_items" in p else "")
                  + (f"  [target {p['target']}]" if p.get("target") else ""))
            if p.get("notes"):
                print(f"    {p['notes']}")
            if p.get("look") and getattr(a, "pcmd", None) == "add":
                print("\n".join(look_setup_lines(p["name"], p["look"])))
        return
    if c == "queue":
        _print_queue(res)
        return
    if c == "expert":
        return render_expert(res)
    if c == "launch":
        it = f"#{res['item']['id']} {res['item']['title']}"
        if res.get("dry_run"):
            print(f"would start {res['agent']} in {res['project']} ({res['path']}) for {it}, model "
                  f"{res.get('model') or 'its default'} ({res.get('model_why') or 'none named'})"
                  + (f": {res['why']}" if res.get("why") else ""))
            if res.get("would_push_to"):
                print(f"  no terminal: {res['would_push_to']} waits for work in {res['project']} and would get it")
            else:
                print("  command: " + "".join(f"{k}={v} " for k, v in res["env"].items()) + res["command"]
                      + "   (in a new tmux pane)")
        elif res.get("pushed_to"):
            print(f"gave {it} to {res['pushed_to']}, which was waiting for work in {res['project']}")
        else:
            print(f"started {res['agent']} as {res['session_name']} in {res['project']} for {it}, model "
                  f"{res.get('model') or 'its default'} ({res.get('model_why') or 'none named'})"
                  + (f" ({res['why']})" if res.get("why") else "")
                  + (f"; tmux pane {res['tmux_pane']}: maxpm view shows it" if res.get("tmux_pane") else ""))
            if res.get("via_serve"):
                print("  maxpm serve opened it: this session runs in a sandbox")
        return
    if c == "view" and "orphans" in res:
        for o in res["orphans"]:
            print(f"{'ended' if o.get('ended') else ('could not end' if res['ended'] else 'left behind')}: "
                  f"tmux server PID {o['pid']} (session {o['session']}, {core._short(core.timedelta(seconds=o['age']))} old; socket gone: {o['socket']})")
        if not res["orphans"]:
            print("(no tmux server left behind)")
        elif not res["ended"]:
            print(f"maxpm view --orphans --tidy ends {'it' if len(res['orphans']) == 1 else 'them'}")
        return
    if c == "view":
        for p in res["closed"]:
            print(f"closed ({p['why']}): {p['name']}")
        for p in res["panes"]:
            print(f"{p['pane']:>4}  {p['name']}" + (f"  [{p['agent']}]" if p["agent"] else "")
                  + (f"  ({p['done']}: maxpm view --tidy closes it)" if p["done"] else ""))
        if not res["panes"]:
            print(f"(no agent panes in the tmux session {res['session']})")
        for name in res["left"]:
            print(f"no space to show it beside the others; it keeps its own window: {name}")
        if res["panes"] and a.layout:
            print("This command has no terminal of its own, so it shows nothing here. A person sees the agents with: maxpm view")
        return
    if c == "interrupt":
        sent = res.get("sent")
        if res.get("command"):  # a second Stop (#5563): its command, after the keys only when the turn still ran
            print(f"{res['agent']}: {' '.join(res['keys'] + [res['command']])} in its terminal, to {res['did']}")
            return
        print(f"{res['agent']}: its turn was stopped ({' '.join(res['keys'])} in its terminal)"
              + (f"; the message went at once: {sent['text']}" if sent else "; it waits at its prompt")
              + (f". {res['again']}: maxpm interrupt {res['agent']}" if res.get("again") else ""))
        return
    if c == "stop" and "killed" in res:
        print(f"{'killed' if res['killed'] else 'found dead'}: {res['agent']} (PID {res['pid']}); released "
              + (", ".join(f"#{h['id']}" for h in res["released"]) or "no items")
              + (f"; queued items back: {', '.join('#' + str(i) for i in res['queued_back'])}" if res["queued_back"] else "")
              + f". {res['warning']}")
        return
    if c == "stop":
        print(f"asked {res['agent']} to stop ({res['reason']}); it ends {res['ends']}"
              + (f"; native delivery: {res['native']}" if res.get("native") else "")
              + ("; it holds " + ", ".join(f"#{h['id']}" for h in res["holds"]) if res["holds"] else ""))
        return
    if c == "target":
        if isinstance(res, list):
            if not res:
                print("(no targets: maxpm target add <name> --description \"how it deploys\")")
            for t in res:
                print(f"{t['name']}  ({t['projects']} project{'' if t['projects'] == 1 else 's'})"
                      + (f"  owner {t['owner']}" if t["owner"] else ""))
                if t["description"]:
                    print(f"    {t['description']}")
            return
        if a.tcmd == "rename":
            print(f"target {res['was']} is now {res['name']}: {res['projects']} projects and {res['items']} deploy "
                  f"and review items follow"
                  + (f"; its deploy project is now {res['deploy_project']}" if res["deploy_project"] else ""))
            print(f"  the old name {res['was']} does not work from now on; agents keep their names, and a new agent "
                  "gets the new name")
            return
        if a.tcmd == "cut":
            d, ships = res["cut"], res["items"]
            if res.get("moved"):
                print(f"release #{d['id']} of {res['name']} is cut already: it builds from {res['moved']} now, and the "
                      f"release-cut hook runs again with that revision")
                return
            print(f"release #{d['id']} of {res['name']} is cut with {len(ships)} item{'' if len(ships) == 1 else 's'}: "
                  f"a later ship request joins the next deploy item, which starts after this release is done")
            for x in ships:
                print(f"    #{x['id']} {x['title']} ({x['status'].replace('_', ' ')})")
            return
        if a.tcmd == "hook":
            if res["changed"]:
                print(f"hook {res['event']} of target {res['target']} {res['changed']}")
            print("\n".join(_hook_lines(res["target"], res["hooks"])))
            if res["hooks"]:
                print(f"  each runs in {res['folder'] or 'the folder of the command that causes the event'}, for at "
                      f"most {res['timeout']} (hook_timeout), with MAXPM_EVENT, MAXPM_TARGET, MAXPM_REV, MAXPM_ITEM, "
                      f"and MAXPM_AGENT set")
            if any(h["event"] == "deploy" for h in res["hooks"]):
                t = res["target"]
                print(f"  deploy: maxpm serve runs it when the deploy item is ready, for at most {res['deploy_timeout']} "
                      f"(deploy_timeout); no session takes the deploy item. A failure keeps it open, adds a fix item, "
                      f"and the release starts again")
                if res.get("review") != "on":
                    print(f"  the review is off for {t}: the hook runs with no review. Turn it on: "
                          f"maxpm config set review on --project deploy-{t}")
            free = [e for e in res["events"] if e not in {h["event"] for h in res["hooks"]}]
            if free:
                print(f"  set one: maxpm target hook {res['target']} <event> \"<command>\"   events with no hook:")
                for e in free:
                    print(f"    {e}: {res['events'][e]}")
            return
        if a.tcmd == "release-now":
            it = res["item"]
            print(f"release now: deploy #{res['deploy']} for {res['target']} goes out sooner "
                  f"({res['early']}); the history of #{res['deploy']} records it")
            print(f"  it starts with #{it['id']} {it['title']}" + (
                ", which is ready" if it["ready"] else ", which waits on "
                + ", ".join(f"#{b}" for b in it["open_blockers"]) if it["open_blockers"] else f" ({it['status']})"))
            for x in res["ships"]:
                print(f"    ships #{x['id']} {x['title']} ({x['status'].replace('_', ' ')})")
            return
        print(res["name"])
        if a.tcmd == "add":
            print("  set its release gate and cadence from the needs of its projects (a service with users: a short "
                  "cadence, so a fix is live soon; a public tool repository: each push); the commands are below")
        print("  " + (res["description"] or f"(no description: maxpm target describe {res['name']} \"how it deploys\")"))
        checks = res.get("checks") or []
        when = (f"run at {res['watch_times']} after each deploy" if res.get("watch_times") else
                f"run at once after each deploy (times after it: maxpm target watch {res['name']} 1m 10m 1h 24h)")
        print("  release watch: " + (", ".join(checks) + " " + when if checks else
                                     f"none (no check of the target or its projects: maxpm ops add {res['name']} <check> <form> ...)"
                                     + (f"; times {res['watch_times']} after each deploy" if res.get("watch_times") else "")))
        modes = {"launch": "launch (maxpm serve alerts the owner when a deploy is ready, and starts a deployer session "
                           "when the owner cannot take it)",
                 "standing": "standing (maxpm serve keeps a deployer session that owns the target while a release "
                             "waits on its review)",
                 "off": "off (maxpm serve starts nothing; the owner or a person deploys)"}
        if any(h["event"] == "deploy" for h in res.get("hooks") or []):
            mode = f"not used: the deploy hook releases, whatever the mode ({res['deployer']}) (#5066)"
        else:
            mode = modes[res["deployer"]]
        print(f"  deployer: {mode}; change it: maxpm target deployer {res['name']} launch|standing|off")
        for line in _release_lines(res["release"]):
            print("  " + line)
        for line in _hook_lines(res["name"], res.get("hooks") or []):
            print("  " + line)
        if res["owner"]:
            left = core._short(core.parse_iso(res["owner_expires_at"]) - core.now())
            print(f"  owner: {res['owner']} ({left} left; any command by {res['owner']} renews it)")
        else:
            print(f"  owner: nobody (take it: maxpm target own {res['name']})")
        print(f"  projects ({len(res['projects'])}):")
        for p in res["projects"]:
            print(f"    {p['name']}  ({p['open_items']} open)")
        if not res["projects"]:
            print(f"    (none: maxpm project target <project> {res['name']})")
        return
    if c == "backlog" and "backlog" in res:
        rows = res["backlog"]
        if not rows:
            print("(the backlog is empty)")
        project = None
        for b in rows:
            if b["project"] != project:
                project = b["project"]
                print(f"{project}:")
            print(f"  #{b['id']:<4} P{b['priority']} {b['title']}" + (f"  ({b['doer']})" if b["doer"] != "any" else "")
                  + (f"  [goal {','.join(b['goals'])}]" if b["goals"] else ""))
            print(f"         why not now: {b['backlog_note'] or '-'} (since {b['backlog_at'][:10]})"
                  + (f"; waited on by {', '.join('#' + str(w) for w in b['waited_by'])}" if b["waited_by"] else ""))
        if rows:
            print("Take one up: maxpm activate <id>")
        return
    if c == "backlog":
        print(f"#{res['id']} {res['title']}: moved to the backlog ({res['note']}). Nobody is offered it until "
              f"maxpm activate {res['id']}.")
        if res["waiting"]:
            print("  These open items wait on it and cannot be ready meanwhile: "
                  + ", ".join("#" + str(w) for w in res["waiting"]))
        return
    if c == "activate":
        print(f"#{res['id']} {res['title']}: open again, "
              + ("ready." if res["ready"] else "waits on " + ", ".join("#" + str(b) for b in res["open_blockers"])
                 if res["open_blockers"] else "not ready yet (maxpm show " + str(res["id"]) + ")."))
        return
    if c in ("list",):
        if not res:
            print("(no items)")
        for it in res:
            print(_fmt_item(it, show_reason=it["ready"]))
        return
    if c == "next":
        if not res:
            print(HINTS["empty"])
            return
        if a.claim:
            print("Claimed:")
            _print_show(res[0])
        else:
            for it in res:
                print(_fmt_item(it))
                for line in _context_lines(it, "       "):
                    print(line)
        return
    if c == "blockers":
        _print_tree(res)
        held = []
        def walk(n):
            for ch in n["children"]:
                if ch["assignee"] and ch["assignee"] != a.actor:
                    held.append(ch)
                walk(ch)
        walk(res)
        if held:
            h = held[0]
            print(f"\nHeld by another agent? Offer help: maxpm offer \"I am blocked on this; I can take ...\" --item {h['id']}"
                  f"   Ready pieces nobody holds: maxpm next --unblocks {res['id']} --claim")
        return
    if c == "status":
        return render_status(res)
    if c == "notify":
        if "subscribe" in res:
            print("\n".join(res["subscribe"]))
            return
        if "results" in res:
            if not res["results"]:
                print("(nothing to send)")
            for r in res["results"]:
                if r["sent"]:
                    print(f"{r['channel']}: sent {r['rows']} in one message ({r['title']})")
                elif r.get("error"):
                    print(f"{r['channel']}: failed for {r['rows']}: {r['error']} (retries on the next run)")
                else:
                    print(f"{r['channel']}: {r['rows']} waiting for the batch window ({r['waiting']} left; --now sends them)")
        elif "channels" in res:
            print(f"interval {res['interval']}, batch window {res['batch_window']}")
            if not res["channels"]:
                print("(no channels: maxpm config set notify_channels log)")
            for ch in res["channels"]:
                notes = [] if ch["adapter"] else ["no adapter"]
                if not ch["configured"]:
                    notes.append("not in notify_channels")
                print(f"{ch['channel']}: {ch['pending']} pending, last sent {ch['last_sent'] or 'never'}"
                      + (f", last error: {ch['last_error']}" if ch["last_error"] else "")
                      + (f" ({'; '.join(notes)})" if notes else ""))
            if res.get("hint"):
                print(res["hint"])
            from .notify import OPEN_TEXT
            print(f"a tap opens {OPEN_TEXT[res['open']]} (notify_open {res['open']}"
                  + "".join(f"; {p}: {v}" for p, v in res["open_people"].items()) + ")")
            if res.get("phone"):
                print(res["phone"])
        else:
            print(f"{res['channel']}: " + ("test sent" if res["ok"] else f"failed: {res['error']}"))
        return
    if c == "prompt":
        print(res["prompt"])
        return
    if c == "needs-you":
        if not res:
            print("(nothing needs a person now)")
        for e in res:
            who = e["human"] or "any person"
            state = f"closed {e['closed_at']} ({e['close_reason']})" if e["closed_at"] else f"open since {e['opened_at']}"
            pri = ""
            if e.get("priority") is not None:
                pri = f"P{e['priority']}" + (f" from #{e['priority_from']}" if e["priority_from"] else "")
                if e["unblocks_count"]:
                    pri += f", unblocks {e['unblocks_count']}"
                pri = f"[{pri}] "
            print(f"[{e['id']}] {pri}for {who}: {e['summary']}  ({state})")
            for n in e["notifications"]:
                print(f"      {n['channel']}: {n['state']}" + (f" after {n['attempts']} tries: {n['last_error']}" if n["last_error"] else ""))
        return
    if c == "log":
        window = f"last {res['since']}" if res["since"] else "all time"
        if not res["items"]:
            print(f"(nothing done in the {window})" if res["since"] else "(nothing done yet)")
        for d in res["by_day"]:
            print(f"{d['day']}  ({len(d['items'])} done)")
            for it in d["items"]:
                by = f" by {it['by_agent']}" if it["by_agent"] not in (None, "?") else ""
                print(f"  #{it['id']:<4} [{it['project']}] {it['title']}  ({it['closed_at'][11:16]}{by})")
                if it["output"]:
                    print(f"        {it['output']}")
        if res["progress"]:
            print("\nProgress:")
            w = max(len(p["project"]) for p in res["progress"])
            for p in res["progress"]:
                pct = round(100 * p["done"] / p["total"]) if p["total"] else 0
                bar = "#" * round(pct / 5) + "." * (20 - round(pct / 5))
                recent = f", +{p['done_in_window']} in the {window}" if res["since"] else ""
                print(f"  {p['project']:<{w}}  {bar} {pct:>3}%  {p['done']}/{p['total']} done{recent}")
        return
    if c in ("offer", "decline") and "kind" in res and "body" in res:
        print(f"{'sent' if c == 'offer' else 'declined'} #{res['id']} {res['kind']}" + (f" to {res['to_agent']}" if c == "offer" else ""))
        return
    if c == "send" or (c in ("alert", "ask", "note") and isinstance(res, list)):
        for m in res if isinstance(res, list) else [res]:
            to = m["to_agent"] or f"the next holder of #{m['item_id']} (nobody holds it now)"
            if m.get("manager_wait") and m["kind"] in ("alert", "question"):
                # The place where a sender learns the flag (#1608): six of ten such messages came from a
                # sender that went on with its item (#1597).
                more = (f" (the manager's watch brings it within {m['manager_wait']}; with --blocked at once: use it "
                        f"when your item cannot move until the answer)")
            else:
                more = f" (native: {m['native_status']})" if m.get("native_status") else ""
            print(f"sent #{m['id']} {m['kind']} to {to}" + (", blocked" if m.get("blocked") else "") + more)
            if m.get("held_note"):
                print(f"  {m['held_note']}")
            if m.get("then"):
                print(f"  Now: {m['then']}")
        return
    if c == "accept" and a.message:
        print(f"accepted alert #{res['id']}" + (f"; see: maxpm show {res['item_id']}" if res["item_id"] else "")
              + "; the sender is told")
        return
    if c == "answer":
        if res.get("native_status") in ("in the chat", "by the asker"):
            print(f"question #{res['reply_to']} is closed with the answer of {res['from_agent']} that you have")
            return
        # #5303: an asker that waits at its prompt gets the answer in its session.
        if res.get("native_status") == core.ANSWER_TO_PANE:
            print(f"answered question #{res['reply_to']}: {res['to_agent']} waits at its prompt, and maxpm serve types "
                  f"the answer into its terminal when its idle prompt shows")
            return
        if res.get("native_status") == "sent":
            print(f"answered question #{res['reply_to']}: the answer went into the session of {res['to_agent']}")
            return
        print(f"answered: sent #{res['id']} to {res['to_agent']}; question #{res['reply_to']} is closed")
        return
    if c == "withdraw":
        print(f"question #{res['withdrawn']} is withdrawn: {res['note']}")
        return
    if c == "forward":
        if a.item is not None:
            print(f"#{res['id']} {res['title']} went on to the person: their Waits for you, needs-you and phone show it now")
        elif a.drop:
            print(f"alert #{res['id']} is closed: {res['to_agent']} does not see it")
        else:
            print(f"{res['kind']} #{res['id']} went on to {res['to_agent']}: their Waits for you, needs-you and phone show it now")
        return
    if c == "inbox":
        if isinstance(res, dict):  # --wait
            if res["result"] == "stop":
                print("STOP REQUESTED" + (f": {res['stop']['stop_reason']}" if res["stop"].get("stop_reason") else ""))
            elif res["result"] == "timeout":
                print(f"(no new message in {res['waited']})")
            else:
                print(f"NEW MESSAGES for {res['agent']}:" + (f" (and {res['still_open']} older, still open: maxpm "
                                                             f"--as {res['agent']} inbox)" if res.get("still_open") else ""))
                # Delivered, not read (#5685): the sender sees that until the session acts on them.
                print(f"  (delivered, not read: maxpm --as {res['agent']} inbox, an answer or a reply marks them read)")
            res = res["messages"]
        elif not res:
            print("(inbox empty)")
        for m in res:
            print(_fmt_msg(m))
        return
    if c == "thread":
        if not res["messages"]:
            print("(no messages)")
        for m in res["messages"]:
            print(_fmt_msg(m, "  " if m["reply_to"] else "", states=True))
        return
    if c == "who":
        if not res:
            print("(nobody)")
        # Experts last, apart from the workers: they take no queue work (#4955).
        for ag in sorted(res, key=lambda g: g.get("role") in ("expert", "assistant")):
            holds = ", ".join(f"#{h['id']} {h['title']}" for h in ag["holds"]) or "nothing"
            note = f" — {ag['note']}" if ag["note"] else ""
            kind = ("expert, takes no queue work" if ag.get("role") == "expert" else
                    f"assistant of {ag.get('assistant_for')}, takes no queue work" if ag.get("role") == "assistant"
                    else ag["kind"])
            print(f"{ag['name']} ({kind}, {ag['state']}){note}"
                  + (f"\n    session: {ag['session']}" + (f" [{ag['session_ref']}]" if ag.get("session_ref") else "")
                     if ag.get("session") else "")
                  + (f"\n    process: PID {ag['pid']} on {ag['host']}" if ag.get("pid") else "")
                  + (f"\n    chat over MCP: {ag['via']}" if ag.get("via") else "") + f"\n    holds: {holds}"
                  + (f"\n    owns: {', '.join(o['name'] for o in ag['owns'])}" if ag.get("owns") else "")
                  + (f"\n    pinned by: {', '.join(ag['pinned_by'])} (top of the Sessions tab)" if ag.get("pinned_by") else "")
                  + (f"\n    {cache_text(ag['cache'], ag['name'])}" if "cache" in ag else ""))
            for t in ag.get("touching", []):
                print(f"    touches: #{t['id']} {', '.join(t['paths'])}")
        return
    if c == "capacity":
        print(f"Ready for agents: {len(res['ready_for_agents'])}   ready for humans: {len(res['ready_for_humans'])}   "
              f"in progress: {len(res['in_progress'])}")
        print(f"Active agent sessions: {res['agents_active']} (busy {res['agents_busy']}, idle {len(res['agents_idle'])})")
        print(f"Spare slots: {res['spare_slots']}   excess sessions: {res['excess_sessions']}   "
              f"peak future width: {res['peak_width']}")
        for adv in res["advice"]:
            print(" -", adv["text"])
        return
    if c == "config":
        if "overrides" in res:
            for k, v in res["defaults"].items():
                print(f"{k} = {v} (default)")
            for o in res["overrides"]:
                print(f"{o['key']} = {o['value']} ({o['scope']})")
        elif "migrated" in res:
            print(res["value"])
            print(res["migrated"])
        else:
            print(json.dumps(res, ensure_ascii=False))
        return
    if c == "now":
        print(f"{res['name']}: " + "; ".join(x for x in (res["headline"] and "work: " + res["headline"],
                                                         res["doing"] and "now: " + res["doing"]) if x))
        return
    if c == "note":
        print(f"{res['name']}: status note set")
        return
    if c == "register":
        print(f"{res['name']} ({res['kind']}) registered. Set MAXPM_AGENT={res['name']} in your shell.")
        return
    if c == "session" and "pin" in res:
        x = res["pin"]
        print(f"{x['agent']}: {'pinned' if x['pinned'] else 'unpinned'} for {x['person']}"
              + (f" (pinned by {', '.join(x['pinned_by'])})" if x["pinned_by"] else ""))
        return
    if c == "session" and "to" in res:
        print(f"session {res['session']} is {res['to']}'s now"
              + (f"; removed from {', '.join(res['from'])}" if res["from"] else ""))
        return
    if c == "session":
        print(f"{res['name']}: session {res['session']}" + (f" [{res['session_ref']}]" if res["session_ref"] else "")
              + " recorded")
        return
    if c == "heartbeat":
        print("leases renewed")
        return
    if c == "report":
        print(f"{res['agent']}: {res['state']}" if res["stored"] else f"{res['agent']}: not stored: {res['why']}")
        return
    if c == "release" and res.get("state") == "gone":
        print(f"{res['agent']}: its session ended (gone)")
        return
    if isinstance(res, dict) and "id" in res and "title" in res:
        _print_show(res)
        return
    print(json.dumps(res, indent=2, default=str))


def main():
    from .client import refuse_native_windows
    refuse_native_windows()
    try:
        sys.exit(run())
    except RiverError as e:
        print(f"{core.COMMAND}: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
