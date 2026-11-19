"""An MCP server (stdio) for agents that cannot run shell commands.

Run it with `maxpm mcp`. Every tool runs the same code as the `maxpm` command
and returns the same text, so an agent reads the same briefings either way.
The server remembers the agent name that `go` or `manage` gives, and
passes it as --as on later calls that do not name one. With MAXPM_CHAT=1 (the
Claude desktop entry that `maxpm setup-agent --claude-desktop` writes) the
session is a chat with no folder: go and manage brief it for that.
Standard library only.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import threading

from . import __version__, cli, core
from .core import RiverError

PROTOCOL = "2025-06-18"

_AS = {"type": "string", "description": "your MaximizePM agent name (default: the one go or plan gave you)"}
BROWSER_ACTIONS = ("open", "snapshot", "find", "outline", "text", "extract", "html", "click", "type", "fill", "select",
                   "check", "uncheck", "key", "dialog", "upload", "back", "forward", "reload", "screenshot", "status",
                   "tab", "new-tab", "close-tab", "steps", "run", "hover", "scroll", "drag", "errors", "size", "record", "image")
TOOLS = [
    {"name": "maxpm",
     "description": "Run any maxpm command and get its text output, for example [\"go\"], "
                    "[\"done\", \"12\", \"--output\", \"what changed, commit id\"], or [\"guide\"].",
     "inputSchema": {"type": "object", "required": ["args"], "properties": {
         "args": {"type": "array", "items": {"type": "string"}, "description": "the words after `maxpm`"},
         "as": _AS,
         "cwd": {"type": "string", "description": "run in this folder (the project folder, for go and add)"}}}},
    {"name": "go",
     "description": "Start or continue work: MaximizePM names you, picks a role, claims an item, and prints a briefing "
                    "that ends with what to run next.",
     "inputSchema": {"type": "object", "properties": {
         "as": _AS, "project": {"type": "string"}, "cwd": {"type": "string", "description": "the project folder"}}}},
    {"name": "done",
     "description": "Finish an item you hold. Put what changed and the commit id in output. A work task in a project "
                    "with code needs its proof: commits (default: the ones that name #id), the test you ran and its "
                    "result, a shot of a visible change; or no_proof with the reason.",
     "inputSchema": {"type": "object", "required": ["id", "output"], "properties": {
         "id": {"type": "integer"}, "output": {"type": "string"}, "as": _AS,
         "commits": {"type": "array", "items": {"type": "string"}},
         "test": {"type": "string", "description": "the test command you ran"},
         "result": {"type": "string", "description": "what it printed last, for example 'OK (911 tests)'"},
         "shots": {"type": "array", "items": {"type": "string"}, "description": "files, or canvas:<name>"},
         "no_proof": {"type": "string", "description": "why the result has no commit or test"}}}},
    {"name": "show",
     "description": "One item with its notes, links, and history.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}, "as": _AS}}},
    {"name": "goal",
     "description": "Goals: outcomes in a project that one agent owns. list (open goals with owner and progress), "
                    "show (a goal and its items), own (you plan and take the items that reach it; refused for a shared "
                    "goal, which has no owner and whose items are open to every agent), release, "
                    "or done (declare it complete with a one-line result; refused while its items are open).",
     "inputSchema": {"type": "object", "required": ["action"], "properties": {
         "action": {"type": "string", "enum": ["list", "show", "own", "release", "done"]},
         "name": {"type": "string", "description": "the goal (all actions except list)"},
         "project": {"type": "string", "description": "list: only this project's goals"},
         "result": {"type": "string", "description": "done: what the goal achieved, one line"},
         "as": _AS}}},
    {"name": "canvas",
     "description": "Canvases: pages (HTML, Markdown, or SVG) that you make for the user and MaximizePM keeps on this "
                    "computer, with versions; the user sees them in the MaximizePM page and comments on a part of a "
                    "page or on the whole page. put (a new version; the same name in the same project is the same "
                    "canvas), list, show (a version's text, its versions and comments), comments (the comments with "
                    "their replies; open: only the ones that wait for you), reply, resolve, open (put it in front of the user: the MaximizePM page opens it in your session's side panel), save (a version as a file), "
                    "section (a doc: replace or add one section of a Markdown canvas; pending shows it as being written), "
                    "answer (a decision of a doc, answered in place), kit (the starter page of the canvas kit: a "
                    "visual HTML page with a task graph, cards, side by side, bars, a timeline, a mockup, pictures "
                    "with notes, and decision cards; fill it and put it), publish (show it off this computer: a "
                    "private realquicksite page; the user shares it there), design (a new design document with the "
                    "outline brief, discovery, decisions, plan, risks), plan (the versions of a design document's "
                    "plan and the state of its tasks).",
     "inputSchema": {"type": "object", "required": ["action"], "properties": {
         "action": {"type": "string", "enum": ["put", "list", "show", "comments", "reply",
                                                "resolve", "open", "save", "section", "answer", "kit",
                                                "publish", "design", "plan"]},
         "heading": {"type": "string", "description": "section: the heading of the section to replace or add (a doc)"},
         "pending": {"type": "string", "description": "section: what the section will say; the page shows it as being "
                                                      "written"},
         "decision": {"type": "integer", "description": "answer: the decision's number (Decision <n> of <m>)"},
         "name": {"type": "string", "description": "the canvas (all actions except list, reply, resolve, kit)"},
         "text": {"type": "string", "description": "put: the page's whole text; reply: the reply"},
         "kind": {"type": "string", "enum": ["html", "md", "svg"], "description": "put: the kind of a new canvas"},
         "title": {"type": "string"}, "note": {"type": "string", "description": "put: what changed; resolve: what "
                                                                                 "changed, or why nothing needs to"},
         "item": {"type": "integer", "description": "put: the item it belongs to (default: the item you hold); "
                                                    "list: only this item's"},
         "project": {"type": "string"}, "version": {"type": "integer"},
         "open": {"type": "boolean", "description": "comments: only the ones that wait for an answer"},
         "comment": {"type": "string", "description": "reply and resolve: the comment id, for example c12"},
         "path": {"type": "string", "description": "save: the file to write"},
         "session": {"type": "string", "description": "open: the agent whose side panel shows it (default: you)"},
         "as": _AS}}},
    {"name": "browser",
     "description": "Your project's browser (maxpm browser): open (replies with the page's outline), snapshot, find, "
                    "outline, text, extract, html, click, type, fill, select, check, uncheck, key, dialog, back, "
                    "forward, reload, screenshot, status, tab, new-tab, close-tab. A target is a name as the "
                    "snapshot prints it, role and name (link \"Comments\"), or a ref (@e3). The reply says what "
                    "changed; json: the reply as {ok, kind, url, title, changed, data, error {code, candidates}}.",
     "inputSchema": {"type": "object", "required": ["action"], "properties": {
         "action": {"type": "string", "enum": list(BROWSER_ACTIONS)},
         "target": {"type": "string", "description": "the element: a name, role \"name\", @e3; tab, close-tab: the tab"},
         "text": {"type": "string", "description": "type, fill: the text; find: the words; dialog: a prompt's answer; "
                                                   "run: the lines of a walk (as steps prints them, {name} for inputs); "
                                                   "record: start, stop, status, or step <n>"},
         "url": {"type": "string", "description": "open, new-tab: the address"},
         "x": {"type": "number"}, "y": {"type": "number"},
         "double": {"type": "boolean"}, "right": {"type": "boolean"},
         "with": {"type": "string", "description": "click, hover, drag: keys held, such as shift or ctrl,alt"},
         "to": {"type": "string", "description": "drag: where it ends (a name, @e3, or x,y)"},
         "amount": {"type": "string", "description": "scroll: down (default), up, left, right, or pixels"},
         "key": {"type": "string", "description": "key: Enter, Tab, Escape, ArrowDown, or one character"},
         "values": {"type": "array", "items": {"type": "string"}, "description": "select: the options; upload: the "
                                                                                 "files (full paths)"},
         "accept": {"type": "boolean", "description": "dialog: true accepts (default), false dismisses"},
         "in": {"type": "string", "description": "the text of the target's row, card or region"},
         "rows": {"type": "string", "description": "extract: the rows (a ref or name in a repeated row, or CSS)"},
         "fields": {"type": "array", "items": {"type": "string"}, "description": "extract: name=<css>[@attr]"},
         "region": {"type": "string"}, "scope": {"type": "string"}, "all": {"type": "boolean"},
         "section": {"type": "string"}, "grep": {"type": "string"}, "limit": {"type": "integer"},
         "wait": {"type": "string", "enum": ["auto", "load", "idle", "none"]},
         "tab": {"type": "string", "description": "the tab to act in (default: the one that shows)"},
         "out": {"type": "string", "description": "image: the file to write (full path; default: the downloads)"},
         "at": {"type": "string", "description": "click, hover, scroll, drag: a point inside the target's box, "
                                                 "30%,70% or 12,40 pixels (screenshot of the target with grid shows it)"},
         "to_at": {"type": "string", "description": "drag: the point inside the to element where it ends"},
         "grid": {"type": "boolean", "description": "screenshot of a target: lines at each 10% of its box"},
         "project": {"type": "string"}, "json": {"type": "boolean"}, "as": _AS}}},
    {"name": "inbox",
     "description": "Your unread messages and the questions that wait for your answer.",
     "inputSchema": {"type": "object", "properties": {"as": _AS}}},
    {"name": "manage",
     "description": "Run the other agents (one manager at a time): what needs attention (stuck agents, projects "
                    "with ready work and no agent, what waits on the user) and the manager's commands.",
     "inputSchema": {"type": "object", "properties": {
         "as": _AS, "takeover": {"type": "string", "description": "why you take over from the active manager"}}}},
]

# ---------------------------------------------------------------- the planner tools (#6190)
# A chat app (Claude, ChatGPT, Gemini CLI, any MCP client) that plans with the person: it adds and changes projects,
# goals, tasks and their links, and reads them back, while the manager runs the work. It acts as the person (the
# history says <person>@<client>, from the client's name in initialize), and it has no tool that takes, finishes,
# deploys, launches or stops work, nor one that runs any command: maxpm mcp --plan, or /mcp/plan in maxpm serve.

_TASK = {"type": "integer", "description": "the task's number, as list_tasks and search_tasks show it (#12 is 12)"}
_PROJECT = {"type": "string", "description": "the project's name, as list_projects shows it"}
_DOER = {"type": "string", "enum": ["agent", "human", "any"],
         "description": "who does it: agent (an AI session), human (the person: a decision, an approval, an account "
                        "step), any (the default)"}
_PRIORITY = {"type": "integer", "minimum": 0, "maximum": 4,
             "description": "0 is the most urgent, 4 the least; 2 is the default. Give a high priority only to the "
                            "outcome: the tasks it waits on take its priority by themselves"}
_TIER = {"type": "string", "enum": ["normal", "expert"],
         "description": "expert: a task the person talks through with an expert session over hours or days; normal: "
                        "any worker (the default)"}
PLAN_TOOLS = [
    {"name": "list_projects",
     "description": "The projects: what each one covers, its open and ready tasks, and its goals.",
     "inputSchema": {"type": "object", "properties": {
         "archived": {"type": "boolean", "description": "the archived projects instead"}}}},
    {"name": "show_project",
     "description": "One project: its description, folder, goals, who works there, and its ready tasks.",
     "inputSchema": {"type": "object", "required": ["name"], "properties": {"name": _PROJECT}}},
    {"name": "add_project",
     "description": "Add a project. The description says what it covers: agents read it to pick their area.",
     "inputSchema": {"type": "object", "required": ["name", "description"], "properties": {
         "name": {"type": "string", "description": "a short name with no spaces, for example billing-site"},
         "description": {"type": "string"},
         "rank": {"type": "integer", "description": "its place among the projects (1 is first)"}}}},
    {"name": "describe_project",
     "description": "Replace a project's description.",
     "inputSchema": {"type": "object", "required": ["name", "description"], "properties": {
         "name": _PROJECT, "description": {"type": "string"}}}},
    {"name": "list_goals",
     "description": "The open goals (outcomes with a done-when test), with their owner and progress.",
     "inputSchema": {"type": "object", "properties": {
         "project": _PROJECT, "all": {"type": "boolean", "description": "the done goals too"}}}},
    {"name": "show_goal",
     "description": "One goal: its outcome, its done-when test, and its tasks.",
     "inputSchema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}},
    {"name": "add_goal",
     "description": "Add a goal to a project: an outcome and the test that shows it is reached. Tag tasks with it "
                    "(add_task goal) so one agent can own the outcome.",
     "inputSchema": {"type": "object", "required": ["project", "name", "outcome"], "properties": {
         "project": _PROJECT,
         "name": {"type": "string", "description": "a short name with no spaces, for example faster-checkout"},
         "outcome": {"type": "string", "description": "what is true when the goal is reached"},
         "done_when": {"type": "string", "description": "the test that shows the outcome holds"},
         "parent": {"type": "string", "description": "a sub-goal of this goal (one level, same project)"},
         "shared": {"type": "boolean", "description": "no owner: several agents work on its tasks at the same time"},
         "rank": {"type": "integer", "description": "its place among the project's goals (1 is first)"}}}},
    {"name": "list_tasks",
     "description": "The open tasks in queue order (the order in which agents take them), with what each waits on.",
     "inputSchema": {"type": "object", "properties": {
         "project": _PROJECT, "goal": {"type": "string", "description": "only the tasks of this goal"},
         "status": {"type": "string", "description": "open (the default), in_progress, held, done, dropped"},
         "limit": {"type": "integer", "description": "at most this many"}}}},
    {"name": "search_tasks",
     "description": "Tasks in any state (open, backlog, done, dropped) that hold every word, newest first. Search "
                    "before add_task: the work may be in the queue already, or done last week.",
     "inputSchema": {"type": "object", "required": ["words"], "properties": {
         "words": {"type": "string"}, "project": _PROJECT,
         "status": {"type": "string", "description": "open, backlog, in_progress, held, done, dropped, closed, or "
                                                     "all (the default)"},
         "since": {"type": "string", "description": "created or closed since: 30d, 2h, or a date"},
         "limit": {"type": "integer", "description": "at most this many (default 20)"}}}},
    {"name": "show_task",
     "description": "One task: its context, what it waits on and unblocks, its goals, its messages and history.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": _TASK}}},
    {"name": "add_task",
     "description": "Add a task. One outcome per task; the context says what a new agent must know to start (why, "
                    "where, what is decided). A task that must wait for others names them in waits_on. When "
                    "MaximizePM finds a task like it, it adds nothing and shows them: add it again with new_reason "
                    "only when it is different work. The manager sees the new task and starts an agent for it.",
     "inputSchema": {"type": "object", "required": ["project", "title"], "properties": {
         "project": _PROJECT,
         "title": {"type": "string", "description": "the whole task in one line: start with a verb"},
         "short": {"type": "string", "description": "about 6 plain words that every page shows"},
         "context": {"type": "string"},
         "goal": {"type": "array", "items": {"type": "string"}, "description": "the goals it works toward"},
         "priority": _PRIORITY, "doer": _DOER, "tier": _TIER,
         "waits_on": {"type": "array", "items": {"type": "integer"}, "description": "tasks that must be done first"},
         "blocks": {"type": "integer", "description": "a task that must wait for this new one"},
         "backlog": {"type": "string", "description": "add it to the backlog with this reason: kept, but nobody takes "
                                                      "it until activate_task"},
         "start": {"type": "string", "description": "it waits until then: tomorrow 9:00, 2026-12-01, 3d"},
         "due": {"type": "string", "description": "a due date: 2026-12-15 or 'fri 17:00'"},
         "new_reason": {"type": "string", "description": "how it differs from the like tasks MaximizePM showed"}}}},
    {"name": "edit_task",
     "description": "Change a task: give only the fields that change.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {
         "id": _TASK, "title": {"type": "string"}, "short": {"type": "string"},
         "context": {"type": "string", "description": "replaces the context"},
         "priority": _PRIORITY, "doer": _DOER, "tier": _TIER,
         "project": {"type": "string", "description": "move it to this project"},
         "add_goal": {"type": "array", "items": {"type": "string"}, "description": "tag it with these goals"},
         "remove_goal": {"type": "array", "items": {"type": "string"}, "description": "remove these goal tags"},
         "due": {"type": "string", "description": "a due date, or none to remove it"}}}},
    {"name": "link_tasks",
     "description": "Make a task wait on other tasks: it is ready only when they are done.",
     "inputSchema": {"type": "object", "required": ["id", "waits_on"], "properties": {
         "id": _TASK, "waits_on": {"type": "array", "items": {"type": "integer"}},
         "kind": {"type": "string", "enum": ["blocks", "feeds", "conflicts"],
                  "description": "blocks: wait (the default); feeds: wait, then read its output; conflicts: no "
                                 "order, never in progress together"}}}},
    {"name": "unlink_tasks",
     "description": "Remove a wait between tasks.",
     "inputSchema": {"type": "object", "required": ["id", "waits_on"], "properties": {
         "id": _TASK, "waits_on": {"type": "array", "items": {"type": "integer"}}}}},
    {"name": "backlog_task",
     "description": "Move an open task to the backlog: kept, but nobody takes it until activate_task.",
     "inputSchema": {"type": "object", "required": ["id", "reason"], "properties": {
         "id": _TASK, "reason": {"type": "string", "description": "why nobody takes it now"}}}},
    {"name": "activate_task",
     "description": "Take a task out of the backlog: it is open again, and an agent takes it when it is ready.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {
         "id": _TASK, "reason": {"type": "string", "description": "why now"}}}},
    {"name": "drop_task",
     "description": "Drop a task that is no longer needed.",
     "inputSchema": {"type": "object", "required": ["id", "reason"], "properties": {
         "id": _TASK, "reason": {"type": "string"}}}},
    {"name": "planning_guide",
     "description": "How to write good tasks: one outcome each, the context, the waits, the priorities, the goals, "
                    "and the backlog. Read it once before you add tasks.",
     "inputSchema": {"type": "object", "properties": {}}},
]
PLAN_NAMES = frozenset(t["name"] for t in PLAN_TOOLS)
PLAN_INSTRUCTIONS = ("MaximizePM is the person's project management system: projects, goals, and tasks with the order "
                     "between them, which AI agents and people work through while a manager agent runs them. In this "
                     "connection you plan with the person: you read and change projects, goals, tasks and their "
                     "links, and you act as the person. You do no task yourself. Call planning_guide once, search "
                     "before you add a task, and tell the person the numbers of the tasks you added.")


def _words(flag, value):
    return [] if value in (None, "", []) else [flag, str(value)]


def plan_argv(name, a):
    """The maxpm commands (lists of words) of one planner tool call. Every value goes in as flag=value or after --,
    so a text that starts with - stays text."""
    def need(key):
        v = a.get(key)
        if v in (None, "", []):
            raise RiverError(f"{name} needs {key}")
        return v

    def task():
        return str(int(need("id")))

    def ids(key):
        return [str(int(x)) for x in need(key)]

    def opt(flag, key):
        v = a.get(key)
        return [] if v in (None, "", []) else [f"{flag}={v}"]
    doer = {"agent": "ai", "human": "human", "any": "any"}
    if a.get("doer") not in (None, "") and a["doer"] not in doer:
        raise RiverError(f"{name}: doer is agent, human, or any")
    if name == "list_projects":
        return [["project", "list"] + (["--archived"] if a.get("archived") else [])]
    if name == "show_project":
        return [["project", "show", "--", need("name")]]
    if name == "add_project":
        return [["project", "add", f"--description={need('description')}"] + opt("--rank", "rank") + ["--", need("name")]]
    if name == "describe_project":
        return [["project", "describe", "--", need("name"), need("description")]]
    if name == "list_goals":
        return [["goal", "list"] + opt("--project", "project") + (["--all"] if a.get("all") else [])]
    if name == "show_goal":
        return [["goal", "show", "--", need("name")]]
    if name == "add_goal":
        return [["goal", "add", f"--outcome={need('outcome')}"] + opt("--done-when", "done_when")
                + opt("--parent", "parent") + opt("--rank", "rank") + (["--shared"] if a.get("shared") else [])
                + ["--", need("project"), need("name")]]
    if name == "list_tasks":
        return [["list"] + opt("--project", "project") + opt("--goal", "goal") + opt("--status", "status")
                + opt("--limit", "limit")]
    if name == "search_tasks":
        return [["search"] + opt("--project", "project") + opt("--status", "status") + opt("--since", "since")
                + opt("--limit", "limit") + ["--"] + str(need("words")).split()]
    if name == "show_task":
        return [["show", task()]]
    if name == "add_task":
        w = ["add", f"--project={need('project')}"]
        w += opt("--short", "short") + opt("--context", "context") + opt("--priority", "priority")
        w += opt("--tier", "tier") + opt("--start", "start") + opt("--due", "due") + opt("--new", "new_reason")
        w += [f"--doer={doer[a['doer']]}"] if a.get("doer") else []
        w += [f"--goal={g}" for g in a.get("goal") or []] or (["--no-goal"] if a.get("goal") == [] else [])
        w += ["--after", *[str(int(x)) for x in a["waits_on"]]] if a.get("waits_on") else []
        w += [f"--blocks={int(a['blocks'])}"] if a.get("blocks") not in (None, "") else []
        w += [f"--backlog={a['backlog']}"] if a.get("backlog") else []
        return [w + [f"--title={need('title')}"]]
    if name == "edit_task":
        w = ["edit"] + opt("--title", "title") + opt("--short", "short") + opt("--context", "context")
        w += opt("--tier", "tier") + opt("--project", "project") + opt("--due", "due")
        w += [f"--doer={doer[a['doer']]}"] if a.get("doer") else []
        w += [f"--goal={g}" for g in a.get("add_goal") or []] + [f"--untag={g}" for g in a.get("remove_goal") or []]
        out = [w + [task()]] if len(w) > 1 else []
        if a.get("priority") not in (None, ""):
            out.append(["prio", task(), str(int(a["priority"]))])
        if not out:
            raise RiverError("edit_task needs a field to change: title, short, context, priority, doer, tier, "
                             "project, add_goal, remove_goal, or due")
        return out
    if name == "link_tasks":
        return [["dep", task(), "--on", *ids("waits_on")] + opt("--kind", "kind")]
    if name == "unlink_tasks":
        return [["undep", task(), "--on", *ids("waits_on")]]
    if name == "backlog_task":
        return [["backlog", task(), f"--note={need('reason')}"]]
    if name == "activate_task":
        return [["activate", task()] + opt("--note", "reason")]
    if name == "drop_task":
        return [["drop", task(), f"--note={need('reason')}"]]
    if name == "planning_guide":
        return [["guide", "manager"]]
    raise RiverError(f"{name} is not a planner tool: this connection plans projects, goals and tasks "
                     f"({', '.join(sorted(PLAN_NAMES))}); an agent that works on tasks connects to /mcp "
                     f"(maxpm mcp) instead")


def writing_tasks(text):
    """The section Writing tasks of the manager guide (planning_guide): the rest is about running agents."""
    m = re.search(r"^## Writing tasks\n.*?(?=^## |\Z)", text, re.M | re.S)
    return m.group(0).strip() if m else text


def client_slug(info):
    """The name of an MCP client (initialize clientInfo.name) as the history writes it after the person's name:
    mark@claude-ai. Letters, digits and -, at most 32; mcp when the client gives none."""
    name = (info or {}).get("name") if isinstance(info, dict) else None
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:32].strip("-")
    return slug or "mcp"


def planner_person():
    """The person a planner connection acts as: the person seen last in this queue."""
    conn = core.connect()
    try:
        r = conn.execute("SELECT name FROM agents WHERE kind='human' ORDER BY last_seen DESC, name LIMIT 1").fetchone()
    finally:
        conn.close()
    if not r:
        raise RiverError("this queue has no person yet: on this computer, run maxpm register <your-name> --human")
    return r["name"]


INSTRUCTIONS = "Call go to take work from the MaximizePM queue, and follow its briefing."
CHAT_INSTRUCTIONS = ("MaximizePM is the user's work queue for AI agents. In this chat (no folder, no shell) you can: "
                     "run the coding agents and plan work with the user (manage), go through what waits on the user "
                     "(maxpm tool: [\"needs-you\"], then [\"prompt\", \"--all\"]), and do items that need no code, "
                     "such as writing or research (go; the result goes in done). Call the tool, then follow its briefing; "
                     "for other maxpm commands use the maxpm tool with the words after `maxpm`.")


def browser_argv(a):
    """#5678: the maxpm browser words of an MCP browser call: the flags, then -- and the positional words (so a text
    that starts with - stays text). json goes before browser: it is a flag of maxpm."""
    act = a.get("action")
    if act not in BROWSER_ACTIONS:
        raise RiverError(f"browser action is one of {', '.join(BROWSER_ACTIONS)}")
    t, text = a.get("target"), a.get("text")

    def need(what, v):
        if v in (None, "", []):
            raise RiverError(f"browser {act} needs {what}")
        return v
    flags = []
    for key, flag in (("in", "--in"), ("wait", "--wait"), ("region", "--region"), ("scope", "--scope"),
                      ("section", "--section"), ("grep", "--grep"), ("rows", "--rows"), ("tab", "--tab"),
                      ("project", "--project"), ("out", "--out"), ("at", "--at"), ("to_at", "--to-at")):
        if a.get(key) not in (None, ""):
            flags += [flag, str(a[key])]
    if a.get("grid"):  # #5689
        flags.append("--grid")
    if a.get("limit"):
        flags += ["--limit", str(int(a["limit"]))]
    if a.get("all"):
        flags.append("--all")
    if a.get("with") and act in ("click", "hover", "drag"):  # #5669
        flags += ["--with", str(a["with"])]
    if act == "click":
        flags += (["--double"] if a.get("double") else []) + (["--right"] if a.get("right") else [])
    pos = []
    if act == "open":
        pos = [need("the address (url)", a.get("url"))]
    elif act == "new-tab" and a.get("url"):
        pos = [a["url"]]
    elif act in ("click", "hover"):
        pos = [str(a["x"]), str(a["y"])] if a.get("x") is not None and a.get("y") is not None else \
            [need("the element (target) or a point (x, y)", t)]
    elif act == "scroll":
        pos = ([t] if t else []) + ([str(a["amount"])] if a.get("amount") else [])
    elif act == "drag":
        pos = [need("where it starts (target)", t), need("where it ends (to)", a.get("to"))]
    elif act == "type":
        pos = ([t] if t else []) + [need("the text (text)", text)]
    elif act == "fill":
        pos = [need("the field (target)", t), text or ""]
    elif act == "select":
        pos = [need("the select (target)", t)] + [str(v) for v in need("the options (values)", a.get("values"))]
    elif act == "upload":
        pos = [need("the file field (target)", t)] + [str(v) for v in need("the files (values)", a.get("values"))]
    elif act == "run":  # #5680: the lines of the walk in text, the inputs (name=value) in values
        flags += ["--lines", need("the lines of the walk (text)", text)]
        pos = [str(v) for v in a.get("values") or []]
    elif act in ("check", "uncheck"):
        pos = [need("the element (target)", t)]
    elif act == "key":
        pos = [need("the key (key)", a.get("key"))]
    elif act == "dialog":
        accept = a.get("accept", True) is not False
        pos = ["accept" if accept else "dismiss"] + ([text] if accept and text is not None else [])
    elif act == "find":
        pos = [need("the words (text)", text)]
    elif act == "size":  # #5676, #5885: fit, phone, tablet, laptop, desktop, or <w>x<h>
        pos = [need("the size (text): fit, phone, tablet, laptop, desktop or <width>x<height>", text)]
    elif act == "record":  # #5682: start, stop, status, step <n>
        pos = (text or "status").split()
    elif act == "image":  # #5688
        pos = [need("the image (target)", t)]
    elif act == "screenshot" and t:  # #5689: one element
        pos = [t]
    elif act == "extract":
        pos = [str(f) for f in a.get("fields") or []]
    elif act in ("tab", "close-tab") and t:
        pos = [t]
    return (["--json"] if a.get("json") else []) + ["browser", act] + flags + (["--"] + pos if pos else [])


def canvas_argv(a):
    """The maxpm canvas words of an MCP canvas call: the same operations as the command (#4648)."""
    act = a.get("action")
    acts = ("put", "list", "show", "comments", "reply", "resolve", "open", "save",
            "section", "answer", "kit", "publish", "design", "plan")
    if act not in acts:
        raise RiverError(f"canvas action is one of {', '.join(acts)}")
    words = ["canvas", act]
    if act in ("reply", "resolve"):
        if not a.get("comment"):
            raise RiverError(f"canvas {act} needs the comment id (comment), for example c12")
        words.append(str(a["comment"]))
    elif act not in ("list", "kit"):
        if not a.get("name"):
            raise RiverError(f"canvas {act} needs the canvas name (name)")
        words.append(str(a["name"]))
    if act == "reply":
        text = a.get("text") or ""
        words.append(" " + text if text.startswith("-") else text)  # argparse reads a leading - as an option
    if act == "save":
        if not a.get("path"):
            raise RiverError("canvas save needs the file to write (path)")
        words.append(" " + a["path"] if a["path"].startswith("-") else a["path"])
    if act == "put":
        if a.get("text") is None:
            raise RiverError("canvas put needs the page's whole text (text)")
        words += [f"--name={words.pop()}", f"--text={a['text']}"]
    if act == "section":
        if not a.get("heading"):
            raise RiverError("canvas section needs the section's heading (heading)")
        words.append(" " + a["heading"] if a["heading"].startswith("-") else a["heading"])
        if a.get("pending") is not None:
            words.append(f"--pending={a['pending']}")
        elif a.get("text") is not None:
            words.append(f"--text={a['text']}")
        else:
            raise RiverError("canvas section needs the section's text (text) or what it will say (pending)")
    if act == "answer":
        if a.get("decision") is None or not a.get("text"):
            raise RiverError("canvas answer needs the decision's number (decision) and the answer (text)")
        words += [str(int(a["decision"])), " " + a["text"] if a["text"].startswith("-") else a["text"]]
    opts = {"put": ("kind", "title", "note", "item", "project"), "list": ("project", "item"),
            "show": ("version", "project"), "comments": ("project",), "resolve": ("note",),
            "section": ("note", "project"), "answer": ("project",), "open": ("project", "session"), "save": ("version", "project"),
            "publish": ("version", "project"), "design": ("title", "item", "project"), "plan": ("version", "project")}
    for k in opts.get(act, ()):
        if act == "save" and k == "path":
            continue
        if a.get(k) not in (None, ""):
            words += [f"--{k}={a[k]}"]
    if act == "comments" and a.get("open"):
        words.append("--open")
    return words


# One river command at a time: a call changes the process's folder and captures its stdout, and maxpm serve
# answers HTTP requests on several threads.
_CALL_LOCK = threading.Lock()


class Server:
    """chat: a session with no folder (default: MAXPM_CHAT=1); go, plan, and manage get --chat. folders: whether a
    tool call may name a cwd (not over HTTP: a web chat has no folder on this computer)."""

    def __init__(self, chat=None, folders=True, plan=False):
        # Over HTTP the session is a new chat: not the agent whose environment started maxpm serve.
        self.agent = os.environ.get("MAXPM_AGENT") if folders and not plan else None
        self.chat = os.environ.get("MAXPM_CHAT") == "1" if chat is None else chat
        self.folders = folders
        self.plan = plan  # the planner tools only (#6190): it acts as the person, as <person>@<client>
        self.client = "mcp"

    def argv(self, name, a):
        if name == "maxpm":
            words = [str(x) for x in a.get("args") or []]
        elif name == "go":
            words = ["go"] + (["--project", a["project"]] if a.get("project") else [])
        elif name == "done":
            words = ["done", str(a["id"]), "--output", a["output"]]
            for c in a.get("commits") or ():  # the proof (#5988)
                words += ["--commit", c]
            for s in a.get("shots") or ():
                words += ["--shot", s]
            for k in ("test", "result", "no_proof"):
                if a.get(k):
                    words += ["--" + k.replace("_", "-"), a[k]]
        elif name == "show":
            words = ["show", str(a["id"])]
        elif name == "goal":
            act = a["action"]
            if act not in ("list", "show", "own", "release", "done"):
                raise RiverError("goal action is one of list, show, own, release, done")
            words = ["goal", act]
            if act == "list":
                words += ["--project", a["project"]] if a.get("project") else []
            else:
                if not a.get("name"):
                    raise RiverError(f"goal {act} needs the goal name")
                words.append(a["name"])
            if act == "done":
                words += ["--result", a.get("result") or ""]
        elif name == "canvas":
            words = canvas_argv(a)
        elif name == "browser":
            words = browser_argv(a)
        elif name == "inbox":
            words = ["inbox"]
        elif name == "manage":
            words = ["manage"] + (["--takeover", a["takeover"]] if a.get("takeover") else [])
        else:
            raise RiverError(f"unknown tool {name!r}")
        if self.chat and words and words[0] in ("go", "manage") and "--chat" not in words:
            words = words[:1] + ["--chat"] + words[1:]
        who = a.get("as") or self.agent
        if who and "--as" not in words:
            words = ["--as", who] + words
        return words

    def plan_call(self, name, a):
        """One planner tool (#6190): its fixed commands, run as the person, recorded as <person>@<client>. A name of
        any other tool is refused (plan_argv), so a planner connection never claims, finishes, deploys or launches."""
        cmds = plan_argv(name, a)
        if self.folders and (os.environ.get(core.TOKEN_ENV) or os.environ.get("MAXPM_AGENT")):
            raise RiverError("the planner acts as the person, and this server runs inside an agent session of "
                             "MaximizePM: the session adds and changes tasks with its own maxpm commands")
        person = planner_person()
        texts, bad = [], False
        with _CALL_LOCK:
            tokens = (core.HTTP_CHAT.set(None if self.folders else "http"), core.PLANNER.set(person),
                      core.EVENT_VIA.set(self.client))
            try:
                for words in cmds:
                    text, bad = self._call(["--quiet", "--as", person] + words, {})
                    # The footer of a command is the person's own state (what they hold, their inbox): not this chat's.
                    texts.append(re.sub(rf"\n?^\[{re.escape(person)}: [^\n]*\]$", "", text, flags=re.M).strip())
                    if bad:
                        break
            finally:
                for var, tok in zip((core.HTTP_CHAT, core.PLANNER, core.EVENT_VIA), tokens):
                    var.reset(tok)
        text = "\n\n".join(texts)
        return (writing_tasks(text) if name == "planning_guide" and not bad else text), bad

    def call(self, name, a):
        """Run one river command in-process; return (text, is_error)."""
        if self.plan:
            return self.plan_call(name, a)
        words = self.argv(name, a)
        if a.get("cwd") and not self.folders:
            raise RiverError("this session has no folder on this computer; leave out cwd")
        with _CALL_LOCK:
            # A chat over HTTP runs inside maxpm serve: go takes nothing from serve's environment (core.HTTP_CHAT).
            token = core.HTTP_CHAT.set(None if self.folders else "http")
            try:
                return self._call(words, a)
            finally:
                core.HTTP_CHAT.reset(token)

    def _call(self, words, a):
        out, err = io.StringIO(), io.StringIO()
        old = os.getcwd()
        # Over HTTP, river's own default name ($MAXPM_AGENT of maxpm serve) is not this chat's.
        env_agent = os.environ.pop("MAXPM_AGENT", None) if not self.folders else None
        code = 0
        try:
            if a.get("cwd"):
                os.chdir(os.path.expanduser(a["cwd"]))
            elif not self.folders or self.plan:  # serve may run in a project folder; a chat (and a planner) is in none
                os.chdir(os.path.abspath(os.sep))
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.run(words) or 0
                except RiverError as e:
                    print(f"maxpm: {e}", file=sys.stderr)
                    code = 2
                except SystemExit as e:  # argparse errors and --help
                    code = e.code if isinstance(e.code, int) else 2
        except OSError as e:
            err.write(f"maxpm: {e}\n")
            code = 2
        finally:
            os.chdir(old)
            if env_agent is not None:
                os.environ["MAXPM_AGENT"] = env_agent
        text = out.getvalue() + err.getvalue()
        m = re.search(r"^You are (?:MaximizePM agent|the MaximizePM MANAGER) (\S+?)(?: \(|\.)", text, re.M)
        if m:
            self.agent = m.group(1)
        return text.strip() or "(no output)", code != 0

    def handle(self, msg):
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            return None  # a notification, such as notifications/initialized
        if method == "initialize":
            params = msg.get("params") or {}
            self.client = client_slug(params.get("clientInfo"))
            result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "maximizepm-planner" if self.plan else "maximizepm", "version": __version__},
                      "instructions": PLAN_INSTRUCTIONS if self.plan else CHAT_INSTRUCTIONS if self.chat else INSTRUCTIONS}
        elif method == "tools/list":
            result = {"tools": PLAN_TOOLS if self.plan else TOOLS}
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                text, bad = self.call(p.get("name"), p.get("arguments") or {})
            except (RiverError, KeyError, TypeError, ValueError) as e:  # a bad argument, such as an id that is text
                text, bad = f"maxpm: {e}", True
            result = {"content": [{"type": "text", "text": text}], "isError": bad}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None, plan=False):
    """plan: the planner tools only (maxpm mcp --plan, #6190)."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    srv = Server(plan=plan)
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = srv.handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


# ---------------------------------------------------------------- Streamable HTTP (maxpm serve, /mcp)

HTTP_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
# Headers that proxies and tunnels add: a request that has one did not come from this computer directly.
PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip", "cf-connecting-ip", "cf-ray",
                 "cf-ipcountry", "true-client-ip")


def _host_only(value):
    v = (value or "").strip().lower()
    if v.startswith("["):
        return v[1:v.find("]")] if "]" in v else v
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def local_refusal(client_ip, headers, what="/mcp"):
    """Why a /mcp request is refused, or None. Until the endpoint has OAuth (#438) it answers only requests
    made on this computer, straight to maxpm serve: never through a tunnel or proxy, whose requests also
    arrive from 127.0.0.1. The Host and Origin checks stop a web page of another site from calling it (DNS
    rebinding). Every other route of maxpm serve (what) follows the same rule: server.Handler._refuse."""
    h = {k.lower(): v for k, v in headers.items()}
    if client_ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return f"only this computer may call {what}"
    if "tailscale-user-login" in h:  # tailscale serve forwarded it from the person's tailnet (#5863)
        return tailnet_refusal(h, what)
    if any(x in h for x in PROXY_HEADERS):
        return f"{what} has no sign-in yet, so it refuses requests through a tunnel or proxy"
    if _host_only(h.get("host")) not in LOCAL_HOSTS:
        return f"{what} answers only on 127.0.0.1 or localhost"
    origin = h.get("origin")
    if origin and _host_only(origin.split("://", 1)[-1].split("/", 1)[0]) not in LOCAL_HOSTS:
        return "cross-origin request refused"
    return None


# The headers that tailscale serve may add besides its own (Tailscale-User-*): any other proxy header means a proxy
# in front of it, and the request is refused.
TAILNET_PROXY_OK = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")


def tailnet_refusal(h, what):
    """Why a request that tailscale serve forwarded (it has Tailscale-User-Login) is refused, or None (#5863). It
    passes only when its host is this computer's tailnet name (Host, or X-Forwarded-Host with a local Host), its
    login is one the person allowed (tailnet.login_ok: the setting tailnet_logins, else the owner of this computer),
    and an Origin is that name too. Tailscale removes that header from a client's request and adds none to a Funnel
    (public) request or a tagged device's, so a request with it came from a person of the tailnet. A local program
    could write the header, but it can call maxpm serve directly anyway. /mcp stays on this computer only: its
    connectors come over Funnel (#5860), which has no login."""
    from . import tailnet
    if what in ("/mcp", "/mcp/plan"):
        return f"{what} answers only on this computer, not over Tailscale"
    if any(x in h for x in PROXY_HEADERS if x not in TAILNET_PROXY_OK):
        return f"{what} refuses a request through another proxy in front of Tailscale"
    me = tailnet.self_info()
    name = me and me.get("name")
    if not name:
        return f"{what}: this computer's Tailscale name is not known (tailscale status --json says none)"
    host, fwd = _host_only(h.get("host")), _host_only(h.get("x-forwarded-host")) if h.get("x-forwarded-host") else None
    if not (host == name and fwd in (None, name)) and not (host in LOCAL_HOSTS and fwd == name):
        return f"{what} answers over Tailscale only at {name}"
    login = (h.get("tailscale-user-login") or "").strip()
    if not tailnet.login_ok(login):
        return (f"{login or 'a login'} may not use {what} over Tailscale: the person adds it with "
                f"maxpm config set tailnet_logins <login>,<login>")
    origin = h.get("origin")
    if origin and _host_only(origin.split("://", 1)[-1].split("/", 1)[0]) not in (name, *LOCAL_HOSTS):
        return "cross-origin request refused"
    return None


def http_post(body, session_id=None, plan=False):
    """One POST to /mcp (plan: /mcp/plan, the planner tools): returns (status, reply or None, headers). A JSON-RPC
    request gets its reply; a notification or a response gets 202 and no body. initialize starts a session
    (Mcp-Session-Id); a session works only on the endpoint that started it."""
    import secrets
    try:
        msg = json.loads(body or b"")
    except ValueError:
        return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, {}
    if not isinstance(msg, dict):
        return 400, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "send one JSON-RPC message per request"}}, {}
    headers = {}
    if msg.get("method") == "initialize":
        session_id = secrets.token_urlsafe(24)
        with _SESSIONS_LOCK:
            HTTP_SESSIONS[session_id] = Server(chat=True, folders=False, plan=plan)
        headers["Mcp-Session-Id"] = session_id
    with _SESSIONS_LOCK:
        srv = HTTP_SESSIONS.get(session_id) if session_id else None
    if srv is not None and srv.plan != plan:  # a planner session never reaches the agent tools, nor the reverse
        srv = None
    if srv is None:
        if not session_id:
            return 400, {"jsonrpc": "2.0", "id": msg.get("id"),
                         "error": {"code": -32600, "message": "no Mcp-Session-Id: initialize first"}}, {}
        return 404, {"jsonrpc": "2.0", "id": msg.get("id"),
                     "error": {"code": -32001, "message": "unknown session: initialize again"}}, {}
    if "method" not in msg or msg.get("id") is None:
        return 202, None, headers  # a notification, or a response to a request of ours (we send none)
    return 200, srv.handle(msg), headers


def http_delete(session_id, plan=False):
    with _SESSIONS_LOCK:
        srv = HTTP_SESSIONS.get(session_id or "")
        return srv is not None and srv.plan == plan and HTTP_SESSIONS.pop(session_id) is not None
