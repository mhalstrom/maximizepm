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
picks a role (owner, worker, unblocker, reviewer, deployer, monitor, planner,
or idle), claims an item, and prints a briefing. Run it again after each item. To plan work with the user instead,
run `maxpm plan`: an overview, the open questions, and the planner's rules.

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
  maxpm guide planner  how to split work into items and dependencies
  maxpm guide setup    how to set up your agents to use MaximizePM
  maxpm --help         every command
"""

# The block maxpm init puts in AGENTS.md: only what an agent needs before its first maxpm command.
# How to work (keep going, wait, report) is in the go and plan briefings, which update with maxpm.
AGENT_SNIPPET = """## Work queue

This project uses MaximizePM (`maxpm`) to track work and who is doing it.
When the user says "go" (or asks you to take work from the queue), run
`maxpm go` in this folder and follow the briefing it prints, to its end.
When the user says "plan", run `maxpm plan` and follow its briefing.
"""

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
     ln -s <repo>/skills/maxpm-planner ~/.claude/skills/maxpm-planner

4. Give each agent session its own name (maxpm register <name>). A person
   registers with --human and usually wants longer claims:
     maxpm config set lease_ttl 7d --agent <person>

5. Watch it:  maxpm serve --open
   Sessions that MaximizePM starts (maxpm launch, the page's Start, fresh sessions)
   open in tmux when tmux is installed (maxpm view shows them all in one
   terminal), else in a Terminal tab. To choose: maxpm config set launch_in
   tmux|tab|window   (auto is the default).

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
    n_open, n_done = len(g["items_open"]), len(g["items_done"])
    state = "complete" if g["status"] == "complete" else (f"owner {g['owner']}" if g["owner"] else
                                                          "shared: no owner" if g.get("shared") else "no owner")
    return (f"{g['name']} [{g['project']}] ({state}; {n_done} done, {n_open} open"
            + (f"; sub-goal of {g['parent']}" if g.get("parent") else "")
            + (f"; sub-goals {', '.join(g['subgoals'])}" if g.get("subgoals") else "") + ")"
            + (f": {g['outcome']}" if g["outcome"] else ""))


def _fmt_item(a, show_reason=True):
    flags = []
    if a["status"] != "open":
        flags.append(a["status"] + (f" by {a['assignee']}" if a.get("assignee") else ""))
    elif a["open_blockers"]:
        flags.append("waits on " + ",".join(f"#{b}" for b in a["open_blockers"]))
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
            parts.append(f"{label}{a[f]}" + (f" ({src})" if src == "agent_rules" else f" ({src} default)" if src and src != "item" else ""))
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
    if not p["cadence"]:
        out = [f"release cadence: off (a release starts when the one before it is done; set one: "
               f"maxpm target cadence {name} 2h|1d|1w|1mo)"] + ([p["text"]] if p["text"] else [])
    else:
        out = [f"{p['text']}; change it: maxpm target cadence {name} <time>|off"]
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


def _ship_line(p):
    """For the worker that asked for a ship: when its change goes out (the target's cut and release cadence)."""
    if p.get("in_cut"):
        return "a review of the release that is cut waits on this item: it goes out with that release"
    return p["text"] + (f"; your change goes out with the next release (deploy #{p['deploy']}). The target owner or "
                        f"the manager decides a release sooner (maxpm target release-now {p['target']} --reason "
                        f"\"<why>\")" if p["waits"] else "")


def _print_show(a):
    print(_fmt_item(a))
    if a.get("kind") == "deploy":
        print(f"  deploy item for target {a['target']} (owner: {a.get('target_owner') or 'nobody'}; only the owner takes it)")
    if a.get("kind") == "deploy" and a.get("cut_at"):
        print(f"  release cut {a['cut_at'][:16].replace('T', ' ')} UTC: it holds exactly these items; a later ship request "
              f"joins the next deploy item")
    if (a.get("release") or {}).get("text") or (a.get("release") or {}).get("in_cut"):  # maxpm ship
        print(f"  {_ship_line(a['release'])}")
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
    if a.get("output"):
        print("  output:", a["output"])
    if a.get("found_during"):
        print(f"  found during: #{a['found_during']}")
    if a.get("found_here"):
        print("  found while doing this:", ", ".join(f"#{d['id']} {d['title']} ({d['status']})" for d in a["found_here"]))
    if a.get("message_count"):
        print(f"  messages: {a['message_count']} (maxpm thread --item {a['id']})")
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
    for e in a.get("events", [])[:8]:
        print(f"  {e['at']} {e['actor']}: {e['change']}")


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


def _handoff_lines(goal, h, due=None, indent="  "):
    """The handoff of a goal for goal show and the go briefing; and a line when its owner should renew it."""
    out = []
    if h:
        out.append(f"{indent}HANDOFF v{h['version']} ({h['by_agent'] or '?'}, {h['created_at']}):")
        out += [f"{indent}  {line}" if line else "" for line in (h.get("text") or "").splitlines()]
    else:
        out.append(f"{indent}handoff: none yet (maxpm goal handoff {goal} --file <path>)")
    if due:
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
    if bits:
        print(f"[{actor}: {'; '.join(bits)}]", file=sys.stderr)


def _fmt_msg(m, indent=""):
    to = m["to_agent"] or (f"the holder of #{m['item_id']}" if m["item_id"] else "?")
    about = f" about #{m['item_id']}" + (f" {m['item_title']}" if m.get("item_title") else "") if m["item_id"] else ""
    head = f"{indent}#{m['id']} {m['kind']} from {m['from_agent']} to {to}{about}  ({m['created_at']}"
    if m["kind"] in ("question", "offer") or m["state"] not in ("open", "read"):
        head += f", {m['state']}"
    if m.get("level_set"):
        head += f", level {m['level']}"
    if m.get("blocked"):  # with what MaximizePM saw, when the sender did not set the flag
        head += {"waits": ", blocked (the sender waits for messages)",
                 "item": ", blocked (the sender released or blocked its item)"}.get(m["blocked"], ", blocked")
    if m["unread"]:
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


def build_parser():
    p = argparse.ArgumentParser(prog=core.COMMAND, description="MaximizePM: a dependency-ordered work queue for agents and people.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--as", dest="actor", default=os.environ.get("MAXPM_AGENT"), help="agent name (default $MAXPM_AGENT)")
    p.add_argument("--quiet", "-q", action="store_true", default=bool(os.environ.get("MAXPM_QUIET")),
                   help="no hint lines")
    sub = p.add_subparsers(dest="cmd")

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
    x = prs.add_parser("archive"); x.add_argument("name")
    prs.add_parser("list")

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

    tg = sub.add_parser("target", help="deploy targets: where projects ship to")
    tgs = tg.add_subparsers(dest="tcmd", required=True)
    x = tgs.add_parser("add"); x.add_argument("name")
    x.add_argument("--description", default="", help="how and where it deploys")
    x = tgs.add_parser("describe", help="set how a target deploys"); x.add_argument("name"); x.add_argument("text")
    x = tgs.add_parser("monitor", help="what a session watches after each deploy (health links, logs, for how long)")
    x.add_argument("name"); x.add_argument("text", nargs="?", help="the monitor text; \"\" removes it; none shows it")
    x = tgs.add_parser("deployer", help="what maxpm serve starts for the target's deploys: launch (the default) "
                       "alerts the owner when a deploy is ready, and starts a deployer session when there is no owner "
                       "or the owner cannot take it (gone, or idle at its prompt); standing starts that session as "
                       "soon as a release has a deploy item, so it waits during the review and deploys the moment the "
                       "review passes; off starts nothing")
    x.add_argument("name"); x.add_argument("mode", nargs="?", choices=core.DEPLOYER_MODES, help="none shows it")
    x = tgs.add_parser("cadence", help="the shortest time between two normal releases of the target (2h: at most 12 "
                       "a day; 6h, 1d, 1w, 1mo; off: each release starts at once): ship requests collect on the open "
                       "deploy item, and its review (the deploy item when there is no review) is ready at the end of "
                       "the last release plus the cadence")
    x.add_argument("name"); x.add_argument("cadence", nargs="?", help="a number and m, h, d, w, or mo; off; none shows it")
    x = tgs.add_parser("cut", help="fix the list of items of the collected release now (the target owner, the "
                       "manager, or a person): later ship requests join the next deploy item, which waits until this "
                       "release is done. The cut also comes by itself when a reviewer takes the review of the release")
    x.add_argument("name")
    x.add_argument("--rev", help="the commit that the release builds from, or any word for it: it goes into the history "
                   "and the notes of the deploy item")
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
    x.add_argument("--check", default="", help="command that shows it works (tests, a build)")
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

    x = sub.add_parser("edit", help="change title, notes, doer, or project")
    x.add_argument("id", type=int); x.add_argument("--title"); x.add_argument("--notes")
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
    x.add_argument("--limit", "-n", type=int, default=20, help="how many items to list (default 20)")
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
    x = sub.add_parser("plan", help="start a planner session: overview, open questions, and the planner's rules")
    x.add_argument("--project", help="project name(s) to focus on (default: this folder's, else all)")
    x.add_argument("--chat", action="store_true", default=os.environ.get("MAXPM_CHAT") == "1",
                   help="a chat app session with no folder (default $MAXPM_CHAT=1)")

    x = sub.add_parser("launch", help="start an agent session in a new terminal tab, window, or tmux pane, like the page's Start")
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
    g = x.add_mutually_exclusive_group()
    g.add_argument("--tab", dest="launch_in", action="store_const", const="tab")
    g.add_argument("--window", dest="launch_in", action="store_const", const="window")
    g.add_argument("--tmux", dest="launch_in", action="store_const", const="tmux",
                   help="in a pane of the tmux session 'maxpm' (maxpm view shows them); no Terminal app needed")
    x.add_argument("--dry-run", action="store_true", help="say what it would start; open nothing")
    x = sub.add_parser("view", help="show every agent that runs in tmux (launch_in tmux) side by side in this terminal")
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
    x = sub.add_parser("release", help="give a claimed item back"); x.add_argument("id", type=int); x.add_argument("--note")
    x.add_argument("--author", metavar="COMMITS", help="a release review only: you wrote commits of the release (say "
                   "which); maxpm go and maxpm serve give you this review no more, and another session takes it")
    x = sub.add_parser("drop", help="close an item without doing it"); x.add_argument("id", type=int)
    x.add_argument("--note", help="why (required when an agent drops a person's item)")
    x = sub.add_parser("takeover", help="an agent does a person's item itself (the user is told, and can undo)")
    x.add_argument("id", type=int); x.add_argument("--note", required=True, help="how you will do it without the user")
    x = sub.add_parser("undo-takeover", help="give a taken-over item back to the people"); x.add_argument("id", type=int)
    x = sub.add_parser("reopen", help="open a closed item again"); x.add_argument("id", type=int)
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
    x = sub.add_parser("keep", help="hold an item again while you do its open prerequisites"); x.add_argument("id", type=int)
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
    x = sub.add_parser("session", help="record the Claude Code session you run in: maxpm session \"<name>\" --ref <ref>")
    x.add_argument("name", nargs="+", help="the name ListAgents prints; put it in quotes when it has spaces or starts with #")
    x.add_argument("--ref", help="the short code in brackets after the name in ListAgents")
    x = sub.add_parser("unregister", help="remove an agent that holds nothing"); x.add_argument("name")
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
    sk = sub.add_parser("skills", help="install the agent guides as Claude Code skills")
    sks = sk.add_subparsers(dest="scmd", required=True)
    x = sks.add_parser("install", help="link the guides (maxpm, maxpm-planner, maxpm-manager) into ~/.claude/skills")
    x.add_argument("--dir", default="~/.claude/skills", help="skills folder (default ~/.claude/skills)")
    x.add_argument("--copy", action="store_true", help="copy the files instead of linking them")
    x.add_argument("--force", action="store_true", help="replace a folder that is not a link (your edits there are lost)")
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
    x = sub.add_parser("guide", help="how to use MaximizePM: worker loop, planner, or agent setup")
    x.add_argument("which", nargs="?", default="worker",
                   choices=["worker", "planner", "manager", "setup", "decisions"])
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


def claude_desktop_config_path():
    """Where the Claude desktop app keeps its MCP servers."""
    if sys.platform == "darwin":
        return Path("~/Library/Application Support/Claude/claude_desktop_config.json").expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path("~/AppData/Roaming").expanduser(), "Claude",
                    "claude_desktop_config.json")
    return Path("~/.config/Claude/claude_desktop_config.json").expanduser()


def _toml_river_block(entry):
    """The [mcp_servers.maxpm] tables for an entry. A JSON string is also a valid TOML basic string."""
    lines = ["[mcp_servers.maxpm]", f"command = {json.dumps(entry['command'])}",
             "args = [" + ", ".join(json.dumps(a) for a in entry["args"]) + "]", "", "[mcp_servers.maxpm.env]"]
    lines += [f"{k} = {json.dumps(v)}" for k, v in entry["env"].items()]
    return "\n".join(lines) + "\n"


def _toml_without_river(text):
    """The config text without its [mcp_servers.maxpm] table and subtables; every other line stays."""
    import re
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        m = re.match(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$", line)
        if m:
            name = m.group(1).replace('"', "").replace("'", "").replace(" ", "")
            skip = name == "mcp_servers.maxpm" or name.startswith("mcp_servers.maxpm.")
        if not skip:
            out.append(line)
    return "".join(out)


def _toml_load(text, f):
    try:
        import tomllib
    except ImportError:  # Python before 3.11: river cannot check the file, so it changes it only by lines
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RiverError(f"{f} is not valid TOML ({e}); fix it first, MaximizePM does not overwrite it")


def setup_codex_config(path=None, remove=False):
    """Add (or remove) river in ~/.codex/config.toml, which the ChatGPT desktop app shares with the Codex CLI.
    Only the [mcp_servers.maxpm] tables change; the old file is kept as config.toml.bak."""
    f = Path(path).expanduser() if path else Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "config.toml"
    old = f.read_text() if f.exists() else ""
    before = _toml_load(old, f)
    had = before is not None and "maxpm" in (before.get("mcp_servers") or {}) or "[mcp_servers.maxpm]" in old
    base = _toml_without_river(old)
    if remove:
        if not had:
            return f"{f} has no MaximizePM entry; nothing changed"
        new = base
    else:
        entry = claude_desktop_entry()
        if before is not None and (before.get("mcp_servers") or {}).get("maxpm") == entry:
            return f"MaximizePM is already in {f}; nothing changed"
        new = base.rstrip("\n") + ("\n\n" if base.strip() else "") + _toml_river_block(entry)
    after = _toml_load(new, f)
    if after is not None:
        others = lambda d: {k: v for k, v in (d or {}).items() if k != "mcp_servers"}
        servers = lambda d: {k: v for k, v in ((d or {}).get("mcp_servers") or {}).items() if k != "maxpm"}
        if others(after) != others(before) or servers(after) != servers(before) or (
                not remove and after["mcp_servers"]["maxpm"] != entry):
            raise RiverError(f"MaximizePM could not add its entry to {f} without changing other settings; add it by hand: "
                             f"codex mcp add maxpm --env MAXPM_CHAT=1 -- maxpm mcp")
    if old:
        f.with_name(f.name + ".bak").write_text(old)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(new)
    if remove:
        return f"took MaximizePM out of {f} (the old file: {f.name}.bak). Restart the ChatGPT desktop app."
    return (f"added MaximizePM to {f}" + (f" (the old file: {f.name}.bak)" if old else "") + ".\n"
            "Restart the ChatGPT desktop app, then use Work or Codex mode (plain Chat mode does not reach servers "
            "on this computer). Ask it, for example: \"plan my next work with MaximizePM\" or \"what waits on me in MaximizePM?\". "
            "Codex CLI sessions see MaximizePM too; in a project folder they work as usual.")


def claude_desktop_entry():
    """The mcpServers entry: this Python and this river by full path (the app does not have the shell's
    PATH), the queue this command uses, and MAXPM_CHAT=1 (a chat has no folder)."""
    import shutil
    script = Path(__file__).resolve().parent.parent / "bin" / "maxpm"
    if script.is_file():
        cmd, args = sys.executable, [str(script), "mcp"]
    elif shutil.which("maxpm"):
        cmd, args = str(Path(shutil.which("maxpm")).resolve()), ["mcp"]
    else:
        raise RiverError("cannot find the maxpm program to give the Claude desktop app; install MaximizePM first")
    return {"command": cmd, "args": args,
            "env": {"MAXPM_DB": str(core.db_path().expanduser().resolve()), "MAXPM_CHAT": "1"}}


def setup_claude_desktop(path=None, remove=False):
    """Add (or remove) the river entry in the app's config; other servers and settings stay. The old file
    is kept as <name>.bak when it changes."""
    f = Path(path).expanduser() if path else claude_desktop_config_path()
    old = f.read_text() if f.exists() else ""
    try:
        data = json.loads(old) if old.strip() else {}
    except ValueError as e:
        raise RiverError(f"{f} is not valid JSON ({e}); fix it first, MaximizePM does not overwrite it")
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise RiverError(f"{f}: expected an object with an mcpServers object; fix it first")
    servers = data.setdefault("mcpServers", {})
    if remove:
        if "maxpm" not in servers:
            return f"{f} has no MaximizePM entry; nothing changed"
        del servers["maxpm"]
    else:
        entry = claude_desktop_entry()
        if servers.get("maxpm") == entry:
            return f"MaximizePM is already in {f}; nothing changed"
        servers["maxpm"] = entry
    new = json.dumps(data, indent=2) + "\n"
    if old:
        f.with_name(f.name + ".bak").write_text(old)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(new)
    if remove:
        return f"took MaximizePM out of {f} (the old file: {f.name}.bak). Quit and reopen the Claude desktop app."
    return (f"added MaximizePM to {f}" + (f" (the old file: {f.name}.bak)" if old else "") + ".\n"
            "Quit and reopen the Claude desktop app. Then ask it, for example: \"plan my next work with MaximizePM\", "
            "\"what waits on me in MaximizePM?\", or \"take a writing item from MaximizePM\".")


def install_skills(dest, copy=False, force=False):
    """Link (or copy) the packaged guides into a skills folder. Returns one line per skill."""
    import shutil
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for name in ("maxpm", "maxpm-planner", "maxpm-manager"):
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


def run(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd is None:
        print(QUICKSTART)
        return 0
    if args.cmd == "guide":
        if args.which == "setup":
            print(SETUP)
        elif args.which == "decisions":
            print(core.DECISION_FORMAT)
        else:
            name = {"planner": "maxpm-planner", "manager": "maxpm-manager"}.get(args.which, "maxpm")
            text = (GUIDES / name / "SKILL.md").read_text()
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
    if args.cmd == "setup-agent":
        if args.claude_desktop and args.codex:
            raise RiverError("one app at a time: --claude-desktop or --chatgpt-desktop")
        if args.claude_desktop:
            print(setup_claude_desktop(args.config, args.remove))
            return 0
        if args.codex:
            print(setup_codex_config(args.config, args.remove))
            return 0
        if args.remove or args.config:
            raise RiverError("--remove and --config go with --claude-desktop or --chatgpt-desktop")
        if not args.append:
            print(AGENT_SNIPPET)
            return 0
        f = Path(args.append)
        old = f.read_text() if f.exists() else ""
        if core.names_product(old):
            print(f"{f} already mentions {core.PRODUCT}; nothing added")
            return 0
        f.write_text(old + ("\n" if old and not old.endswith("\n") else "") + ("\n" if old else "") + AGENT_SNIPPET)
        print(f"added the work queue block to {f}")
        return 0
    conn = core.connect()
    try:
        return _run(args, conn)
    finally:
        conn.close()


def _run(args, conn):
    if args.cmd == "serve" and args.restart:
        res = serve_restart(conn)
        print(f"maxpm serve started again and answers on port {core.setting(conn, 'serve_port')}"
              + ("" if not res["stale"] else "; the code changed again since: run it once more")
              if res["restarted"] else
              "maxpm serve took the request but did not answer again within 60s: look at its terminal")
        return 0 if res["restarted"] else 1
    if args.cmd == "serve":
        from . import server
        port = args.port or int(core.setting(conn, "serve_port"))
        server.serve(port, open_browser=args.open, dev=args.dev)
        return 0
    actor = args.actor
    if args.cmd not in ("go", "plan"):
        core.activity(conn, actor)
    st = core.stop_request(conn, actor) if args.cmd not in ("go", "wait", "stop") else None
    if st and not args.json:
        print(_stop_banner(actor, st), file=sys.stderr)
    core.record_claude_session(conn, actor, core.claude_session_from_env())  # before done measures the item
    res = dispatch(conn, args, actor)
    monitors = None
    if args.cmd in ("go", "claim", "next") and core.pending_monitors(conn):
        monitors = ask_server_for_monitors(conn)
        if isinstance(res, dict) and args.cmd == "go":
            res["monitors_opened"] = monitors
    me = res.get("agent") if args.cmd in ("go", "plan", "manage") and isinstance(res, dict) else actor
    core.record_session_url(conn, me, core.session_url_from_env())
    core.record_claude_session(conn, me, core.claude_session_from_env())
    with core.tx(conn):
        core.sync_needs_you(conn)  # the command may have made a human item ready, or sent a question to a person
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
    if monitors is not None and args.cmd != "go" and not args.json:
        for line in _monitor_lines(monitors):
            print(line, file=sys.stderr)
    sys.stdout.flush()
    if not (args.cmd in ("wait", "unregister") and conn.execute("SELECT 1 FROM agents WHERE name=?", (actor,)).fetchone() is None):
        _footer(conn, res["agent"] if args.cmd in ("go", "plan") else actor)
    if not args.quiet and not args.json:
        h = _hint(args, res, actor)
        if h:
            print(h, file=sys.stderr)
    return 0


def _record_process(conn, name):
    """The agent CLI process that runs this command (for maxpm who and maxpm stop --kill)."""
    if not name or not conn.execute("SELECT 1 FROM agents WHERE name=? AND kind='ai'", (name,)).fetchone():
        return
    pid, cmd = core.agent_process()
    if pid:
        core.set_process(conn, name, pid, cmd)


def ask_server_for_monitors(conn, timeout=5):
    """A deploy item was just claimed and its target has a monitor text: ask the maxpm serve of this queue
    to open a session for the monitor item. Returns what it opened, or {"error": ...} when no server runs."""
    try:
        return ask_server(conn, "open_monitors", {}, timeout=timeout)
    except RiverError as e:
        return {"error": str(e), "pending": core.pending_monitors(conn)}


def in_sandbox():
    """This command runs in an agent CLI's sandbox (Claude Code sets SANDBOX_RUNTIME, Codex CODEX_SANDBOX):
    it cannot script Terminal or reach the tmux socket, so it cannot open a session itself."""
    return bool(os.environ.get("SANDBOX_RUNTIME") or os.environ.get("CODEX_SANDBOX"))


RETRY_WAIT = 1.0  # seconds between two tries to reach a maxpm serve that does not listen


def ask_server(conn, op, args, actor=None, timeout=5, retries=3):
    """Ask the maxpm serve of this queue to do one page action (server.OPS) and return its result; RiverError
    says why not. The server runs outside any sandbox. A sandbox refuses a direct connection to this computer;
    its HTTP proxy passes the request when the sandbox allows the host, so the sandbox still decides."""
    import base64
    import urllib.error
    import urllib.request
    from urllib.parse import unquote, urlsplit
    port = core.setting(conn, "serve_port")
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
            try:
                why = json.loads(text)["error"]  # maxpm serve refused: its own words
            except (ValueError, KeyError, TypeError):
                why = None
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
    old = ask_server(conn, "serve_restart", {}, timeout=90)["boot"]
    for _ in range(int(wait)):
        sleep(1)
        try:
            st = ask_server(conn, "serve_status", {}, retries=0)
        except RiverError:
            continue  # it starts
        if st["boot"] != old:
            return {"restarted": True, "stale": st["stale"]}
    return {"restarted": False, "stale": True}


def _monitor_lines(m):
    """What happened to the monitor of a deploy that just started."""
    if isinstance(m, dict):
        return [f"monitor #{p['id']}: no session opened ({m['error']}). Start one in a folder of target "
                f"{p['target']}: MAXPM_FOCUS=monitor:{p['id']} claude go   (or another agent: maxpm go reads MAXPM_FOCUS)"
                for p in m.get("pending", [])]
    return [f"monitor #{x['id']}: " + (f"no session opened: {x['error']}" if x.get("error") else
            f"maxpm serve opened {x['agent']} in {x['project']}" + (f" ({x['model']})" if x.get("model") else "")
            + " to follow the deploy") for x in m]


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
        return f"next: maxpm --as {actor} go   (now: keep going while go gives you items, unless auto_continue is off)"
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
    old = f.read_text() if f.exists() else ""
    if core.names_product(old):
        return f"{f.name}: already has the work queue block"
    f.write_text(old + ("\n" if old and not old.endswith("\n") else "") + ("\n" if old else "") + AGENT_SNIPPET)
    return f"{f.name}: added the work queue block"


def _imports_agents(text):
    """CLAUDE.md that imports AGENTS.md (a line @AGENTS.md): Claude Code reads the same rules as other agents."""
    return any(line.strip() == "@AGENTS.md" for line in text.splitlines())


def _only_block(text):
    return not text.replace(AGENT_SNIPPET, "").strip()


def instructions_layout(folder):
    """How a folder's agent instructions are laid out:
    shared: CLAUDE.md imports AGENTS.md, so every agent reads one file.
    none: no rules (no file, or only the work queue block).  agents_only: the rules are in AGENTS.md.
    claude_only: the rules are in CLAUDE.md, and AGENTS.md is missing or holds only the work queue block.
    both: each file holds its own rules."""
    c, a = Path(folder, "CLAUDE.md"), Path(folder, "AGENTS.md")
    ct = c.read_text(errors="replace") if c.exists() else None
    at = a.read_text(errors="replace") if a.exists() else None
    if ct is not None and _imports_agents(ct):
        return "shared"
    c_rules = ct is not None and not _only_block(ct)
    a_rules = at is not None and not _only_block(at)
    return "both" if c_rules and a_rules else "claude_only" if c_rules else "agents_only" if a_rules else "none"


MOVE_HINT = ("CLAUDE.md holds this folder's rules, but Codex and other agents read AGENTS.md and miss them. "
             "To give every agent one file: maxpm init --move (the text of CLAUDE.md goes to AGENTS.md, and "
             "CLAUDE.md becomes the one line @AGENTS.md, which Claude Code reads as an import)")


def setup_instructions(folder, move=None):
    """Add the work queue block so that every agent reads it, and, when asked, one set of rules for all.

    AGENTS.md holds the rules and the block; CLAUDE.md imports it with @AGENTS.md. move=True moves the
    rules from CLAUDE.md into AGENTS.md; move=False keeps both files as they are (the block goes in each);
    move=None does what needs no choice and says the choice. Returns one line per change."""
    folder = Path(folder)
    c, a = folder / "CLAUDE.md", folder / "AGENTS.md"
    layout = instructions_layout(folder)
    out = []
    if layout == "claude_only" and move:
        a.write_text(c.read_text())
        out += ["AGENTS.md: now holds the rules from CLAUDE.md", _append_block(a)]
        c.write_text("@AGENTS.md\n")
        out.append("CLAUDE.md: now the one line @AGENTS.md, so Claude Code and every other agent read the same rules")
        return out
    if layout in ("none", "agents_only", "shared"):
        if layout != "shared" and c.exists():  # CLAUDE.md holds only the block: AGENTS.md has it now
            c.write_text("@AGENTS.md\n")
            out.append("CLAUDE.md: now the one line @AGENTS.md (it held only the work queue block)")
        out.append(_append_block(a))
        if not c.exists():
            c.write_text("@AGENTS.md\n")
            out.append("CLAUDE.md: created as the one line @AGENTS.md, so Claude Code reads AGENTS.md too")
        return out
    out += [_append_block(c), _append_block(a)]
    if layout == "claude_only":
        out.append(MOVE_HINT if move is None else "AGENTS.md: kept apart from CLAUDE.md; it holds only the work queue block")
    else:
        out.append("CLAUDE.md and AGENTS.md hold different rules, and each agent reads only one of them. To share "
                   "them: move the rules from CLAUDE.md into AGENTS.md and make CLAUDE.md the one line @AGENTS.md")
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
            ans = input("CLAUDE.md holds this folder's rules; Codex and other agents read AGENTS.md. Move the rules "
                        "to AGENTS.md and make CLAUDE.md import it (@AGENTS.md), so every agent reads them? [Y/n] ")
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


def dispatch(conn, a, actor):
    c = a.cmd
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
        return core.project_list(conn)
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
                    text = Path(a.file).expanduser().read_text()
                except OSError as e:
                    raise RiverError(f"cannot read {a.file}: {e.strerror}")
            return core.goal_handoff(conn, a.name, text, actor, a.version)
        if g == "done":
            return core.goal_done(conn, a.name, a.result, actor, a.drop_open)
        if g == "reopen":
            return core.goal_reopen(conn, a.name, actor)
    if c == "launch":
        from . import server
        opts = {}
        for o in a.option:
            k, sep, v = o.partition("=")
            if not sep:
                raise RiverError(f"--option {o!r} needs the form name=value, for example remote_control=off")
            opts[k.strip()] = v.strip()
        if a.dry_run:
            t = core.launch_target(conn, a.project, a.agent, a.item, a.model, a.effort, a.launch_in,
                                   spread=a.project is None and a.item is None, options=opts or None, actor=actor,
                                   prompt=a.prompt)
            return {**t, "dry_run": True, "would_push_to": None if t["custom_prompt"] else
                    core.waiting_agent_for(conn, t["project"], t["item"]["id"])}
        if in_sandbox():
            # A sandbox blocks Terminal and tmux for this command. maxpm serve runs outside it and opens the
            # session, as for the page's Start and Dispatch; a terminal tab can take a while to open.
            if a.prompt is not None:
                core.custom_prompt(a.prompt)  # refuse here, before the request
            choice = {"agent": a.agent, "model": a.model, "effort": a.effort, "launch_in": a.launch_in, "options": opts,
                      "prompt": a.prompt}
            res = (ask_server(conn, "dispatch_item", {"id": a.item, **choice}, actor, timeout=40) if a.item is not None
                   else ask_server(conn, "launch_agent", {"project": a.project, **choice}, actor, timeout=40))
            return {**res, "via_serve": True}
        if a.item is not None:
            return server.dispatch_item(conn, a.item, agent=a.agent, actor=actor, model=a.model, effort=a.effort,
                                        launch_in=a.launch_in, options=opts or None, prompt=a.prompt)
        return server.launch_agent(conn, a.project, agent=a.agent, actor=actor, model=a.model, effort=a.effort,
                                   launch_in=a.launch_in, options=opts or None, prompt=a.prompt)
    if c == "view":
        from . import server
        if a.orphans:
            return server.tmux_orphans_view(end=a.tidy)
        return server.tmux_view(a.layout, a.tidy, conn)
    if c == "stop":
        if a.kill:
            return core.kill_agent(conn, a.agent, a.reason, actor)
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
        if a.tcmd == "monitor":
            return core.target_show(conn, a.name) if a.text is None else core.target_monitor(conn, a.name, a.text, actor)
        if a.tcmd == "deployer":
            return core.target_show(conn, a.name) if a.mode is None else core.target_deployer(conn, a.name, a.mode, actor)
        if a.tcmd == "cadence":
            return core.target_show(conn, a.name) if a.cadence is None else core.target_cadence(conn, a.name, a.cadence, actor)
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
            text = sys.stdin.read() if a.plan_file == "-" else Path(a.plan_file).expanduser().read_text()
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
                            [] if a.no_goal else a.goal, core.parse_refs(a.ref, a.ref_url), _models(a))
        if a.no_goal_context:
            core.item_goal_context(conn, res["id"], False, actor)
        return res
    if c == "edit":
        if a.unreserve:
            core.cancel_push(conn, a.id, actor)
        if a.goal_context is not None:
            core.item_goal_context(conn, a.id, a.goal_context, actor)
        return core.item_edit(conn, a.id, a.title, a.notes, a.doer, a.project, actor, a.context, a.touches, a.check,
                              a.due, a.goal, a.untag, core.parse_refs(a.ref, a.ref_url), a.unref, _models(a))
    if c == "list":
        return core.item_list(conn, a.project, a.status, a.all, a.goal, a.ref)
    if c == "show":
        return core.item_show(conn, a.id)
    if c == "status":
        from . import relay
        return {**core.status(conn, a.recent), "relay": relay.saved_status()}
    if c == "usage":
        return core.usage_report(conn, a.project, a.since, a.measure)
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
            return notify.setup_ntfy(conn, a.url, a.token, actor)
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
        return {**core.plan(conn, os.getcwd(), actor, a.project), "chat": a.chat}
    if c == "manage":
        if a.watch:
            if not actor:
                raise RiverError("--watch needs the manager's name: maxpm --as <name> manage --watch")
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
        return core.done(conn, a.id, a.output, actor, a.ship, a.note, a.synced, a.force)
    if c == "synced":
        return core.synced(conn, a.id, a.ref, actor)
    if c == "cleanup":
        return core.cleanup(conn, a.project, not a.no_git)
    if c == "wait":
        return core.wait(conn, os.getcwd(), actor, a.project, a.step, focus=os.environ.get("MAXPM_FOCUS"))
    if c == "check":
        return core.check(conn, a.id, a.result, a.note, actor)
    if c == "ship":
        return core.ship(conn, a.id, actor)
    if c == "review":
        if a.rcmd == "pass":
            return core.review_pass(conn, a.id, a.output, actor, os.getcwd(), confirm=a.confirm)
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
    if c == "release":
        return core.release(conn, a.id, a.note, actor, a.author)
    if c == "drop":
        return core.drop(conn, a.id, actor, a.note)
    if c == "takeover":
        return core.takeover(conn, a.id, a.note, actor)
    if c == "undo-takeover":
        return core.undo_takeover(conn, a.id, actor)
    if c == "reopen":
        return core.reopen(conn, a.id, actor)
    if c == "prio":
        return core.item_prio(conn, a.id, a.priority, actor)
    if c == "move":
        return core.item_move(conn, a.id, a.before, a.after, actor)
    if c == "dep":
        return core.dep_add(conn, a.id, a.on, actor, a.kind, a.mode)
    if c == "keep":
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
        return core.answer(conn, a.id, a.text, actor)
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
        if not actor:
            raise RiverError("set MAXPM_AGENT or pass --as <name>")
        return core.set_session(conn, actor, " ".join(a.name), a.ref)
    if c == "unregister":
        return core.unregister(conn, a.name, actor)
    if c == "note":
        if not actor:
            raise RiverError("set MAXPM_AGENT or pass --as <name>")
        return core.agent_note(conn, actor, " ".join(a.words))
    if c == "who":
        res = core.who(conn, a.item, a.project, a.file, os.getcwd(), everyone=a.all)
        if not a.all and a.item is None and a.project is None and a.file is None and not a.json:
            ended = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] - len(res)
            if ended:
                print(f"({ended} stopped or gone agent(s) that hold nothing are hidden: maxpm who --all)", file=sys.stderr)
        return res
    if c == "heartbeat":
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
    if not st["configured"]:
        return "Relay: not connected (maxpm connect)"
    who = f" as {st['account']}" if st.get("account") else ""
    if st["state"] == "connected":
        return f"Relay: connected{who} ({st['url']})"
    if st["state"] == "serve not running":
        return f"Relay: signed in{who}, not connected: maxpm serve is not running"
    err = f": {st['error']}" if st.get("error") else ""
    return f"Relay: signed in{who}, {st['state']}{err}"


def connect_command(args):
    from . import relay
    if args.status:
        print(relay_line(relay.saved_status()))
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
        import webbrowser
        try:
            webbrowser.open(dev["verification_uri_complete"])
        except Exception:
            pass
    print("Waiting for the approval (10 minutes at most)...", flush=True)
    cfg = relay.finish_device(dev, replace=args.replace)
    who = f" as {cfg['account']}" if cfg.get("account") else ""
    print(f"Connected to {cfg['url']}{who}. maxpm serve keeps the connection open: it connects within a few "
          "seconds when it runs (start it with maxpm serve).")
    print("Then open your MaximizePM from any browser: " + cfg["url"] + "/app/")
    return 0


def render_status(res):
    rows = res["projects"]
    if not rows:
        print("(no projects: maxpm project add <name>)")
    else:
        w = max(4, *(len(p["project"]) for p in rows))
        print(f"{'project':<{w}}  {'done':>4} {'open':>4} {'ready':>5} {'working':>7} {'human':>5} {'waiting':>7}")
        for p in rows:
            print(f"{p['project']:<{w}}  {p['done']:>4} {p['open']:>4} {p['ready']:>5} {p['in_progress']:>7} "
                  f"{p['human_waiting']:>5} {p['blocked']:>7}" + (f"  [{p['target']}]" if p["target"] else ""))
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


PLAN_RULES = """You are a PLANNER. Talk with the user about what they want done, then write it into the queue.
Ask the user what outcome they want before you add items.
Change the plan only; do not take or do the work (claims refuse for this session).
  Projects:      {r} project add <name> --description "..." [--path <dir>] [--target <t>] --result-look ask_first|push_first
                 (describe, rank, target). result_look is the user's choice: state your recommendation and its
                 reason, and let the user confirm it. The reply of project add prints the rule.
  Items:         {r} add <project> "<title>" --doer ai|human --context "..." --touches <files> --check "<cmd>"
  Order:         {r} dep <id> --on <id> [--kind feeds|conflicts]   Importance: {r} prio <id> 0 (on the outcome only)
  Model:         --model sonnet|opus|fable --effort low..max (advice); --min-model/--max-model only when a wrong model is costly
  Fix:           {r} edit <id> ...   {r} move <id> --before <id>   {r} drop <id>   {r} blocked <id> --reason "..."
  Progress:      maxpm status   maxpm log --since 7d   maxpm blockers <id>
Ask the user about each open question below that matters to what they want. The full guide: maxpm guide planner
When the user wants work done in this session instead: {r} go"""


def _findings_lines(f, r):
    out = []
    for x in f["stuck"]:
        out.append(f"  STUCK {x['agent']} ({x['state']}, {x['why']}): holds "
                   + (", ".join(f"#{i}" for i in x["holds"]) or "nothing") + (f", {x['queued']} queued" if x["queued"] else "")
                   + f".  Ask it to stop: {r} stop {x['agent']} --reason \"...\"")
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
        out.append(f"  QUESTION #{x['id']} from {x['from_agent']} to {x['to_agent']}: {_cut(x['body'], 70)}")
    if f["human_ready"]:
        out.append("  WAITS ON THE USER: " + ", ".join(f"#{x['id']} {_cut(x['title'], 40)}" for x in f["human_ready"][:6]))
    return out or ["  nothing needs you now"]


def render_manage(b):
    me = b["agent"]
    r = f"maxpm --as {me}"
    if "result" in b:  # --watch
        if b["result"] == "stop":
            print("STOP REQUESTED" + (f": {b['stop']['stop_reason']}" if b["stop"].get("stop_reason") else "")
                  + f". Tell the user what you did, then run {r} go to end the session.")
            return
        lines = _findings_lines(b["findings"], r)
        print(("CHANGED: " + ", ".join(b["new"]) if b["new"] else
               f"NEW MESSAGES ({len(b['messages'])})" if b["messages"] else "NOTHING NEW")
              + (f"; resolved: {', '.join(b['gone'])}" if b["gone"] else ""))
        for m in b["messages"]:
            print(_fmt_msg(m, "  "))
        if b.get("still_open"):
            print(f"  and {b['still_open']} older message(s) still open: {r} inbox")
        if b["new"]:
            print("\n".join(lines))
        elif any(b["findings"].values()) and not b["messages"]:  # the standing findings: reported before
            print(f"  {len(lines)} standing finding(s), reported before: {r} manage lists them")
        print((f"Act on what is new, then start {r} manage --watch again in the background."
               if b["new"] or b["messages"] else f"Start {r} manage --watch again in the background."))
        return
    out = [f"You are the MaximizePM MANAGER {me}" + (f" (you took over from {b['took_over']})" if b.get("took_over") else "") + "."]
    if b["new_name"]:
        out.append(f"Pass --as {me} on every maxpm command.")
    st = b["status"]
    out += ["", "Projects: " + ", ".join(f"{p['project']} {p['ready']} ready/{p['open']} open" for p in st["projects"][:12]
                                          if p.get("open")),
            "", "NEEDS ATTENTION:"] + _findings_lines(b["findings"], r)
    out += ["",
            "Your job: plan with the user, and keep the other agents working together. You take no items.",
            f"  launch:  {r} launch [--project P | --item N] [--model M] [--effort E] [--dry-run]",
            f"  queues:  {r} queue add <agent> <id> [--first] | --message \"...\";  queue list|move|remove",
            f"  message: {r} note|alert|ask <agent> \"...\"",
            f"  stop:    {r} stop <agent> --reason \"...\"   (stuck, or waited longer than wait_too_long {b['wait_too_long']})",
            f"  kill:    {r} stop <agent> --kill --reason \"...\"   emergency only, and only after the user says yes",
            f"  targets: {r} target give <target> --to <agent>",
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
                f"every message that waits), ")
             + f"or after manage_every {b['every']} with one line; start it again each time. It is your only "
               f"watcher: MaximizePM refuses a second wait for your messages."),
            *([] if b.get("chat") or not b.get("native") else [
            "Messages: MaximizePM delivers a blocked one and a person's into this session itself (native_message); "
            "they do not wake the watch. The watch brings each other message after its wait."]),
            "The rules: maxpm guide manager"]
    if b.get("chat"):
        out += [""] + _chat_lines(r, False)[:1] + [
            "  launch still works from a chat: it opens a terminal on this computer with a coding agent."]
    print("\n".join(out))


def render_plan(b):
    me = b["agent"]
    r = f"maxpm --as {me}"
    out = [f"You are MaximizePM agent {me}. Role: PLANNER."
           + (f" Focus: {', '.join(b['projects'])}." if b["projects"] else " Focus: every project.")]
    if core.queue_note():
        out.append(core.queue_note() + ". Tell the user if that is not what they meant.")
    if b.get("chat"):
        out += [""] + _chat_lines(r, False)[:1]
    elif not b.get("folder_has_project", True):
        out += ["",
                f"THIS FOLDER HAS NO PROJECT: {b['cwd']}",
                "  The projects below are other work. Leave them alone unless the user names them.",
                "  If the user's goal is about this folder, create its project first, then add items to it:",
                f"  {r} project add <name> --description \"<what it covers>\" --path {b['cwd']} "
                f"--result-look ask_first|push_first",
                f"  --result-look is how a finished result gets the user's look: {LOOK_RULE}.",
                f"  Tell the user your recommendation and its reason, and take the value the user confirms. With no "
                f"value the project has ask_first."]
    if b["new_name"]:
        out.append(f"Your shell may not keep environment variables, so pass --as {me} on every maxpm command.")
    out += ["", PLAN_RULES.format(r=r)]
    for n, t in (b.get("trackers") or {}).items():
        if t:
            out += ["", f"TRACKER: project {n} uses {t}.",
                    "  Import its open issues with that tool before you plan new work:",
                    "  - Link form <tracker>:<key>: github:owner/repo#12, jira:PROJ-123, linear:ENG-42",
                    "  - Skip an issue MaximizePM has already: maxpm list --ref <tracker>:<key>",
                    f"  - Add the others: {r} add {n} \"<issue title>\" --ref <tracker>:<key> --ref-url <issue link> "
                    "--context \"<what the issue says>\"",
                    "  - Keep the tracker's priority (-p 0..4) and order, and link what must come first: "
                    f"{r} dep <id> --on <id>",
                    "  Ask the user before you import a large backlog, or issues that belong to other people."]
        else:
            out += ["", f"No tracker is recorded for {n}. If the user uses one (Jira, GitHub Issues, Linear...),",
                    f"  record it once, in words an agent can act on: {r} project tracker {n} \"<tracker> <where> via <tool>\""]
    out += ["", "OVERVIEW"]
    print("\n".join(out))
    render_status(b["status"])
    q = b["questions"]
    out = ["", "OPEN QUESTIONS" + ("" if b.get("folder_has_project", True) else " (other projects, not this folder)")]
    def section(title, rows, fmt, limit=10):
        if not rows:
            return
        out.append(f"{title} ({len(rows)}):")
        out.extend("  " + fmt(x) for x in rows[:limit])
        if len(rows) > limit:
            out.append(f"  ... {len(rows) - limit} more")
    section("Projects with no description", q["projects_without_description"],
            lambda n: f"{n}   ({r} project describe {n} \"...\")")
    section("Stuck on something outside the queue", q["stuck"],
            lambda x: f"#{x['id']:<4} [{x['project']}] {_cut(x['title'], 60)}  ({x['blocked_text']})"
                      + (f" (holds up {x['holds_up']})" if x["holds_up"] else ""))
    section("Marked replan: work grew after an agent started (split, re-scope, then maxpm replanned <id>)",
            q["replan"], lambda x: f"#{x['id']:<4} [{x['project']}] {_cut(x['title'], 60)}  "
                                   f"({x['late_prereqs']} prerequisites added while claimed)")
    section("Due dates", q.get("due", []), lambda x: f"#{x['id']:<4} [{x['project']}] {_cut(x['title'], 60)}  "
            f"({'OVERDUE' if x['due_state'] == 'overdue' else 'due soon' if x['due_state'] == 'soon' else 'due'} "
            f"{x['due_text']}; {x['open_before']} open before it)")
    section("May be done or stale (check each, then maxpm check <id> done|partial|open)", q.get("suspect", []),
            lambda x: f"#{x['id']:<4} [{x['project']}] {_cut(x['title'], 60)}  ({', '.join(x['reasons'])})")
    section("Waiting on a human", q["human_waiting"], lambda x: f"#{x['id']:<4} [{x['project']}] {_cut(x['title'])}")
    section("Items with no notes or context", q["items_without_notes"],
            lambda x: f"#{x['id']:<4} [{x['project']}] {_cut(x['title'])}")
    if len(out) == 2:
        out.append("(none)")
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
            "  push_first: the worker pushes, closes the item, and adds an item for the person's look at the result.",
            f"  recommended here: {rec}, because {look['why']}. Also ask_first when a push to main is live at once "
            f"(a site, a public repository).",
            (f"  change it: maxpm config set result_look {other} --project {name}" if look["chosen"] else
             f"  tell the person the recommendation and its reason; store their answer: "
             f"maxpm config set result_look ask_first|push_first --project {name}")]


def _look_rule(b, it, r, short=False):
    """The briefing lines for a look of the user at the finished result. The item's project decides (setting
    result_look, #1668): push first and add an item for the look, or ask first and wait for the yes."""
    ask = b.get("result_look") == "ask_first"
    if short:
        if ask:
            return [f"  the user should look at the result: this project asks first: commit, no push, {r} ask <person> "
                    f"\"Look at ...\" --item {it['id']}, wait for the yes, then push and done"]
        return [f"  the user should look at the result: push, done, then {r} add \"Look at ...\" --doer human "
                f"--found-during {it['id']}"]
    if ask:
        return [f"  - The user should look at the finished result: this project asks first (setting result_look). "
                f"Commit, do not push, and",
                f"    {r} ask <person> \"Look at <result>: <where>. Push?\" --item {it['id']}   then wait at your "
                f"prompt for the yes.",
                f"    MaximizePM keeps your lease while the question is open. After the yes: push, then "
                f"{r} done {it['id']}."]
    return [f"  - The user should look at the finished result: push, {r} done {it['id']}, then "
            f"{r} add \"Look at <result>\" --doer human --found-during {it['id']}.",
            f"    Ask before the push only for public text, a release to production, a step that deletes data or "
            f"costs money, or when the item says so."]


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
    if b.get("focus_note"):
        out += [b["focus_note"], ""]
    if b.get("help_prompt"):
        out += [b["help_prompt"], "",
                f"Your own agent name is {me}; take over a person's item only with their yes: {r} takeover <id> --note \"<how>\".",
                f"Do not take other work in this session unless the person asks: then run {r} go --role worker."]
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
                    + (f" by {o['assignee']}" if o["assignee"] else "")) + ("; human" if o["doer"] == "human" else "")
        out.append(f"  items: {gb['items_done']} done, {len(gb['items_open'])} open"
                   + (": " + ", ".join(f"#{o['id']} {_cut(o['title'], 40)} ({st(o)})" for o in gb["items_open"][:6])
                      if gb["items_open"] else ""))
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
        if it.get("found_during") and it.get("kind") != "monitor":
            out.append(f"  (found during #{it['found_during']}; now it is the most important ready item)")
        if it["notes"]:
            out.append(f"  notes: {it['notes']}")
        out += _context_lines(it)
        for h in b.get("handoffs") or []:  # the context of the item's goals, from their owners
            first, *rest = _handoff_lines(h["goal"], h)
            out += [f"  goal {h['goal']}: {first.strip()}"] + rest
        for name in b.get("shared_goals") or []:
            out.append(f"  goal {name} is shared: it has no owner, and other agents work on its other items at the "
                       f"same time. Tag an item you add for it: {r} add \"<title>\" --goal {name}")
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
        if b["role"] == "deployer":
            t = b["target"]
            if not t["description"]:
                out.append(f"  target {t['name']}: (no description: {r} target describe {t['name']} \"how it deploys\")")
            elif t["description"] != it.get("context"):
                out.append(f"  target {t['name']}: {t['description']}")
            out.append("  ships:")
            for d in b["ships"]:
                out.append(f"    #{d['id']} {d['title']} ({d['status']})")
            if b.get("monitor_item"):
                mi = b["monitor_item"]
                out.append(f"  monitor #{mi['id']} follows this deploy ({mi['status'].replace('_', ' ')}"
                           + (f" by {mi['assignee']}" if mi["assignee"] else "") + "); it alerts you if something fails.")
                out += ["  " + x for x in _monitor_lines(b.get("monitors_opened") or [])]
            elif not t.get("monitor"):
                out.append(f"  no monitor: to have a session follow each deploy: {r} target monitor {t['name']} "
                           f"\"<what to watch, for how long>\"")
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
                    "Deploy as the target description says, run its checks, then put the release id or",
                    f"deployed commit in the output: {r} done {it['id']} --output \"<release id, checks passed>\"",
                    "A step needs a person's go (an approval, a command the auto mode classifier refuses): ask in the",
                    f"queue before you stop at your prompt: {r} ask <person> \"<what to approve>\" --item {it['id']}",
                    "While that question is open, maxpm serve keeps your lease and the target and starts no other",
                    "deployer. The person answers in your terminal or with maxpm answer."]
        if b["role"] == "monitor":
            m = b["monitor"]
            d = m["deploy"]
            who = m["deployer"] or "the target owner"
            out += ["",
                    f"You follow deploy #{d['id']} of target {m['target']} ({d['status'].replace('_', ' ')}"
                    + (f", by {m['deployer']}" if m["deployer"] else "") + "). The context above says what to watch and for how long.",
                    f"Watch that long. Your lease lasts 30 minutes: run {r} heartbeat at least every 20 minutes.",
                    f"All is well: {r} done {it['id']} --output \"<what you checked, for how long, what you saw>\"",
                    "Something fails:",
                    f"  1. {r} alert {m['deployer'] or '<deployer>'} \"<what fails, since when>\" --item {d['id']}",
                    f"  2. {r} add \"Roll back {m['target']} (deploy #{d['id']})?\" --doer human --found-during {it['id']} \\",
                    f"       --context \"<the evidence: links, log lines, numbers; the rollback you propose: the exact commands>\"",
                    f"  3. {r} done {it['id']} --output \"problem: <what>; alerted {who}; rollback proposed in #<new id>\"",
                    f"  Do not roll back yourself: {m['person']} decides."]
        if b["role"] == "reviewer":
            out.append(f"  release {it.get('target')}: the deploy waits on this review. It covers:")
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
                f"Stand at your prompt only for a release to",
                f"    production or a deletion of data: {r} ask <person> \"<what to approve>\" --item {it['id']}   first.",
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
        if b.get("auto_continue"):
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
    elif b["role"] == "planner":
        out.append("NO READY WORK. Your job: plan the project into items that agents and people can take.")
        if b.get("open_items"):
            out.append("Open items that cannot move:")
            for o in b["open_items"]:
                why = o["blocked_text"] if o["blocked_reason"] else (
                    "waits on " + ",".join(f"#{x}" for x in o["open_blockers"]) if o["open_blockers"] else "held")
                out.append(f"  #{o['id']} {o['title']}  ({why})")
        out += [
            "",
            "Steps:",
            "  1. Read the project description and the code or documents it names. Ask the user when the goal is unclear.",
            f"  2. Add items, one checkable outcome each: {r} add <project> \"<title>\" --doer ai|human --notes \"<files, commands, how to know it is done>\"",
            f"  3. Link what must come first: {r} dep <id> --on <id> ...   Set importance on the outcome only: {r} prio <id> 0",
            f"  4. Then run {r} go to take the first item, or stop and let other sessions take them.",
            f"  (Full planning guide: maxpm guide planner)",
        ]
    elif b.get("chat"):
        out += ["NOTHING FOR THIS CHAT NOW.",
                "Tell the user what waits on them (below), and ask what they want to do: plan (maxpm plan), "
                "manage the agents (maxpm manage), or answer what waits on them. Do not wait for work in a chat."]
    else:
        out.append("NOTHING FOR YOU NOW.")
        if b.get("queue_waiting"):
            out.append("Your queue holds items that are not ready yet: " + ", ".join(
                f"#{e['item']} {_cut(e['title'], 40)}" for e in b["queue_waiting"][:5]) + "; maxpm wait wakes you when one is.")
        for h in b.get("held_by_others", []):
            out.append(f"  #{h['id']} {h['title']}  (held by {h['assignee']})")
        out += [
            "",
            f"Wait for work: {r} wait",
            "  Run it in the foreground, not as a background command, and never end your turn at your prompt: a",
            "  blocking wait costs no tokens, and MaximizePM ends an agent that sits idle at its prompt.",
            "  It returns when work is pushed to you, an item gets ready here, or a message comes (give the shell",
            "  command a 10-minute time limit). Then do what it prints: WORK: run go. No work yet: run wait again.",
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
    else:
        print(f"END: no work came in {res['waited']}. MaximizePM "
              + ("kept your registration: " + res["kept"] if res.get("kept") else "released your goals and unregistered you")
              + ". Stop now, and tell the user this session has ended and its tab can be closed.")


def render(a, res):
    c = a.cmd
    if c == "wait":
        return render_wait(res)
    if c == "usage":
        return render_usage(res, a.limit)
    if c == "show" and a.brief:
        return _print_brief(res)
    if c == "cleanup":
        return render_cleanup(res, f"maxpm --as {a.actor}" if a.actor else "maxpm")
    if c == "done" and isinstance(res, dict) and res.get("refs"):
        _print_show(res)
        lines = _writeback_lines(res, f"maxpm --as {a.actor}" if a.actor else "maxpm", res.get("tracker", ""))
        if lines:
            print()
            print("\n".join(lines))
        return
    if c == "release" and isinstance(res, dict) and res.get("hint"):
        _print_show(res)
        print()
        print("HINT: " + res["hint"])
        return
    if c == "go":
        return render_go(res)
    if c == "plan":
        return render_plan(res)
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
    if c == "project":
        if isinstance(res, dict) and "ready_count" in res:
            print(f"{res['name']} (rank {res['rank']})")
            print("  " + (res["description"] or "(no description: maxpm project describe " + res["name"] + " \"...\")"))
            print("  target: " + (res.get("target") or "none (maxpm project target " + res["name"] + " <target>)"))
            print("  tracker: " + (res.get("tracker") or "none (maxpm project tracker " + res["name"] + " \"<tracker> <where> via <tool>\")"))
            if res.get("look"):
                print("\n".join("  " + line for line in look_setup_lines(res["name"], res["look"])))
            print("  items: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in res["counts"].items() if v))
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
            print(f"{p['rank']:>2}. {p['name']}" + (f"  ({p['open_items']} open)" if "open_items" in p else "")
                  + (f"  [target {p['target']}]" if p.get("target") else ""))
            if p.get("notes"):
                print(f"    {p['notes']}")
            if p.get("look") and getattr(a, "pcmd", None) == "add":
                print("\n".join(look_setup_lines(p["name"], p["look"])))
        return
    if c == "queue":
        _print_queue(res)
        return
    if c == "launch":
        it = f"#{res['item']['id']} {res['item']['title']}"
        if res.get("dry_run"):
            print(f"would start {res['agent']} in {res['project']} ({res['path']}) for {it}"
                  + (f": {res['why']}" if res.get("why") else ""))
            if res.get("would_push_to"):
                print(f"  no terminal: {res['would_push_to']} waits for work in {res['project']} and would get it")
            else:
                print("  command: " + "".join(f"{k}={v} " for k, v in res["env"].items()) + res["command"]
                      + f"   (in a new {'tmux pane' if res['launch_in'] == 'tmux' else res['launch_in']})")
        elif res.get("pushed_to"):
            print(f"gave {it} to {res['pushed_to']}, which was waiting for work in {res['project']}")
        else:
            print(f"started {res['agent']} as {res['session_name']} in {res['project']} for {it}"
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
            print(f"target {res['was']} is now {res['name']}: {res['projects']} projects and {res['items']} deploy, "
                  f"review and monitor items follow"
                  + (f"; its deploy project is now {res['deploy_project']}" if res["deploy_project"] else ""))
            print(f"  the old name {res['was']} does not work from now on; agents keep their names, and a new agent "
                  "gets the new name")
            return
        if a.tcmd == "cut":
            d, ships = res["cut"], res["items"]
            print(f"release #{d['id']} of {res['name']} is cut with {len(ships)} item{'' if len(ships) == 1 else 's'}: "
                  f"a later ship request joins the next deploy item, which starts after this release is done")
            for x in ships:
                print(f"    #{x['id']} {x['title']} ({x['status'].replace('_', ' ')})")
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
        print("  " + (res["description"] or f"(no description: maxpm target describe {res['name']} \"how it deploys\")"))
        print("  monitor: " + (res.get("monitor") or f"none (a session follows each deploy when you set one: "
                                                      f"maxpm target monitor {res['name']} \"<what to watch, for how long>\")"))
        print("  deployer: " + {"launch": "launch (maxpm serve alerts the owner when a deploy is ready, and starts a "
                                          "deployer session when the owner cannot take it)",
                                "standing": "standing (maxpm serve keeps a deployer session that owns the target while "
                                            "a release waits on its review)",
                                "off": "off (maxpm serve starts nothing; the owner or a person deploys)"}[res["deployer"]]
              + f"; change it: maxpm target deployer {res['name']} launch|standing|off")
        for line in _release_lines(res["release"]):
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
        return
    if c == "accept" and a.message:
        print(f"accepted alert #{res['id']}" + (f"; see: maxpm show {res['item_id']}" if res["item_id"] else "")
              + "; the sender is told")
        return
    if c == "answer":
        print(f"answered: sent #{res['id']} to {res['to_agent']}; question #{res['reply_to']} is closed")
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
            print(_fmt_msg(m, "  " if m["reply_to"] else ""))
        return
    if c == "who":
        if not res:
            print("(nobody)")
        for ag in res:
            holds = ", ".join(f"#{h['id']} {h['title']}" for h in ag["holds"]) or "nothing"
            note = f" — {ag['note']}" if ag["note"] else ""
            print(f"{ag['name']} ({ag['kind']}, {ag['state']}){note}"
                  + (f"\n    session: {ag['session']}" + (f" [{ag['session_ref']}]" if ag.get("session_ref") else "")
                     if ag.get("session") else "")
                  + (f"\n    process: PID {ag['pid']} on {ag['host']}" if ag.get("pid") else "")
                  + (f"\n    chat over MCP: {ag['via']}" if ag.get("via") else "") + f"\n    holds: {holds}"
                  + (f"\n    owns: {', '.join(o['name'] for o in ag['owns'])}" if ag.get("owns") else ""))
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
                print(f"{k} = {v} (default)" + (
                    (": tmux, because tmux is installed" if core.tmux_path() else
                     ": tab, because tmux is not installed (brew install tmux; Linux: apt install tmux)")
                    if (k, v) == ("launch_in", "auto") else ""))
            for o in res["overrides"]:
                print(f"{o['key']} = {o['value']} ({o['scope']})")
        elif "migrated" in res:
            print(res["value"])
            print(res["migrated"])
        else:
            print(json.dumps(res, ensure_ascii=False))
        return
    if c == "note":
        print(f"{res['name']}: status note set")
        return
    if c == "register":
        print(f"{res['name']} ({res['kind']}) registered. Set MAXPM_AGENT={res['name']} in your shell.")
        return
    if c == "session":
        print(f"{res['name']}: session {res['session']}" + (f" [{res['session_ref']}]" if res["session_ref"] else "")
              + " recorded")
        return
    if c == "heartbeat":
        print("leases renewed")
        return
    if isinstance(res, dict) and "id" in res and "title" in res:
        _print_show(res)
        return
    print(json.dumps(res, indent=2, default=str))


def main():
    try:
        sys.exit(run())
    except RiverError as e:
        print(f"{core.COMMAND}: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
