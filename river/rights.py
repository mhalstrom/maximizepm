"""Who may run each command and page action (#5974; design of talk #5935, mark 2026-11-15).

One table for the commands (COMMANDS) and one for the page actions (OPS): each names the callers that may run it.
A caller is a person (a terminal outside an agent, or the page), the manager (a session that maxpm serve started as
the manager, or that already is one), or a session (any other agent session). The caller comes from the session's
token (#5972) or from the name of a session, never from --as alone: a command inside an agent never runs as a person,
and inside an agent with no name it may only read (READS). cli._run and server's /api/action check the table before
each call; a refusal goes to the run log (runs.refused), and a session that is refused again and again alerts the
manager. A test refuses a command or an action that is missing from a table.

Rules of the queue itself (a target's owner, a goal's owner, a review's authors, a credential's grant) stay in the
functions that do the work: the table says who may ask, the function says whether this one thing may happen."""

PERSON, MANAGER, SESSION = "person", "manager", "session"
ANY = frozenset({PERSON, MANAGER, SESSION})
LEAD = frozenset({PERSON, MANAGER})  # the person, or the manager that works for the person
ONLY_PERSON = frozenset({PERSON})

# command -> callers, or {subcommand: callers} (a subcommand that is not named takes "*", else ANY).
COMMANDS = {
    # project path: go tells a session to link its folder
    "project": {"add": LEAD, "describe": ANY, "path": ANY, "show": ANY, "target": LEAD, "rank": LEAD,
                "archive": LEAD, "restore": LEAD, "list": ANY},
    "browser": {"*": ANY},
    "canvas": {"*": ANY},
    "realquicksite": {"status": ANY, "connect": ONLY_PERSON, "disconnect": ONLY_PERSON},
    "secret": {"*": ANY},  # each credential has its own levels and grants (river/vault.py)
    "goal": {"add": ANY, "list": ANY, "show": ANY, "own": ANY, "give": ANY, "release": ANY,
             "base": ANY, "handoff": ANY, "done": ANY,
             "share": ANY},  # goal_edit lets only a person, the manager, or the goal's owner change it (#6168)
    "schedule": {"*": ANY},
    "ops": {"*": ANY, "setup": LEAD, "profile": LEAD},
    "target": {"add": LEAD, "describe": LEAD, "deployer": LEAD, "watch": LEAD, "cadence": LEAD, "cut": ANY,
               "risk": LEAD, "mode": LEAD, "releaser": LEAD,
               "report": ANY, "hook": LEAD, "release-now": ANY, "show": ANY, "own": ANY, "release": ANY, "give": ANY, "list": ANY,
               "summary": ANY},  # #6493: report.summary_set lets a session write only the summary task it holds
    "pipeline": {"add": LEAD, "env": LEAD, "stage": LEAD, "gate": LEAD, "list": ANY, "show": ANY, "export": ANY,
                 "history": ANY, "start": ANY, "approve": LEAD, "run": ANY, "forge": LEAD, "host": LEAD, "rerun": LEAD,
                 "deps": ANY,  # #6603: a change takes a person or the manager (deps.configure)
                 "stop": LEAD, "hold": LEAD, "unhold": LEAD, "rollback": LEAD, "rerun-failed": LEAD,
                 "fix": ANY, "version": ANY, "item": ANY,  # #6113, #6114, #6116
                 "transition": LEAD, "block": LEAD, "unblock": LEAD, "delay": LEAD, "disable": LEAD,
                 "enable": LEAD, "pass": LEAD, "freeze": LEAD,  # #6139
                 "plugins": ANY, "plugin": ANY,  # #6159
                 "audit": ANY, "correct": ANY,  # #6144: correct checks itself (the line's actor, a person, the manager)
                 "holding": ANY, "release": LEAD,  # #6140
                 "may": ANY,  # #6164: an outside flow asks; the answer acts only on its own run at that stage
                 "accounts": ANY,  # #6147: --check takes a person or the manager (accounts.check_now)
                 "runs": ANY, "move-back": LEAD,  # #6141
                 "run-stage": ANY,  # #6138: runner.run_stage takes an operator, a person or the manager
                 "versions": ANY,  # #6143: --back checks a person or the manager (versions.restore)
                 "follow": ANY,  # #6167: follow.follow checks a person or the manager
                 "triggers": ANY, "trigger": LEAD,  # #6150
                 "keys": ANY,  # #6153: names only; --kept-in takes a person or the manager (keys.kept)
                 "permissions": ANY, "permit": LEAD,  # #6154
                 # #6145: operators.change takes a person, operators.check the pipeline's operators
                 "operators": ANY, "emergency": ANY,
                 "event": LEAD, "report": LEAD, "intake": LEAD},  # #6163: events in from any system
    # #6151: a runner runs agents with the queue's rights: a person or the manager adds, changes and stops them
    # #6166: anyone writes a starter or reads; installs and pins take a person or the manager; the yes, a person
    "plugin": {"list": ANY, "show": ANY, "new": ANY, "add": LEAD, "pin": LEAD, "trust": ONLY_PERSON},
    "runner": {"list": ANY, "runs": ANY, "log": ANY, "add": LEAD, "check": LEAD, "set": LEAD, "remove": LEAD,
               "allow": LEAD, "deny": LEAD, "stop": LEAD, "github": LEAD,  # #6152: it moves trigger rules
               "gitlab": LEAD, "pulumi": LEAD},  # #6172, #6173
    "add": ANY, "artifacts": ANY, "search": ANY, "duplicates": ANY, "merge": ANY, "edit": ANY, "list": ANY,
    "show": ANY, "notify": {"*": ANY, "setup": ONLY_PERSON, "on": LEAD, "off": LEAD}, "prompt": ANY, "needs-you": ANY, "status": ANY,
    "usage": ANY, "runs": ANY, "health": ANY, "log": ANY, "next": ANY, "init": ANY, "go": ANY,
    "manage": ANY,  # becoming the manager: may_manage (serve made this session the manager, or a person)
    "plan": ANY, "launch": ANY, "workspace": ANY,
    "expert": {"start": LEAD, "request": ANY, "adopt": ANY, "end": LEAD, "list": ANY, "fresh": LEAD, "notes": ANY},
    "view": ANY, "stop": LEAD, "interrupt": LEAD, "pause": LEAD, "resume": LEAD, "relay": LEAD,
    "approve": LEAD,  # #6282: the manager answers a session's prompt that it holds for the person
    "approval": ANY,  # the acting session names the person's yes (#5957); the function checks the yes itself
    "scope": {"*": LEAD, "show": ANY},
    "queue": {"add": LEAD, "list": ANY, "remove": ANY, "move": LEAD},
    "claim": ANY, "done": ANY, "wait": ANY, "cleanup": ANY, "check": ANY, "ship": ANY, "review": ANY,
    # add: a session adds the rule that the user states in its chat (the worker guide, section Policies)
    "policy": {"list": ANY, "show": ANY, "add": ANY, "edit": LEAD, "retire": LEAD},
    "release": ANY, "drop": ANY, "takeover": ANY, "reopen": ANY, "backlog": ANY, "activate": LEAD, "prio": ANY,
    "dep": ANY, "push": ANY, "accept": ANY, "alert": ANY, "ask": ANY, "decline": ANY, "offer": ANY,
    "give": ANY, "split": ANY, "keep": ANY, "undep": ANY, "blocked": ANY, "unblock": ANY, "replanned": ANY,
    "blockers": ANY, "send": ANY, "answer": ANY, "forward": ANY, "withdraw": ANY, "release-changes": ONLY_PERSON, "inbox": ANY, "thread": ANY,
    "register": ANY, "session": ANY, "unregister": ANY, "now": ANY, "note": ANY, "who": ANY, "link": ANY, "heartbeat": ANY,
    "report": ANY, "capacity": ANY, "host": ONLY_PERSON, "agent-login": ONLY_PERSON, "phone": ONLY_PERSON,
    "config": {"get": ANY, "set": LEAD, "unset": LEAD},
    "serve": ANY,  # serve --restart: sessions run it after a commit (AGENTS.md)
    "skills": ANY, "activity": ANY, "db": ANY, "mcp": ANY, "connect": ANY, "guide": ANY, "setup-agent": ANY,
}

# The commands that change nothing: a command inside an agent with no name (no token, no --as) may run only these.
READS = {"plugin list", "plugin show", "project show", "project list","canvas list", "canvas show", "canvas comments", "goal list", "goal show",
         "ops list", "ops show", "ops history", "target show", "target list", "target report", "artifacts", "search", "duplicates",
         "list", "show", "needs-you", "status", "usage", "runs", "health", "log", "next", "prompt", "workspace list",
         "expert list", "scope show", "queue list", "policy list", "policy show", "blockers", "thread", "who", "link",
         "capacity", "config get", "db path", "guide", "report", "activity", "view", "cleanup", "browser status",
         "pipeline list", "pipeline show", "pipeline history", "pipeline run", "pipeline version", "pipeline item",
         "pipeline plugins", "pipeline audit", "pipeline runs", "pipeline triggers", "pipeline holding", "pipeline keys", "pipeline permissions",
         "runner list", "runner runs", "runner log"}

# page action -> callers. The page acts as the person; a command that asks serve (cli.ask_server) has the rights of
# the session that its processes prove (vault.session_of).
OPS = {op: ANY for op in (
    "accept", "activate", "answer", "backlog", "block", "browser", "browser_annotate", "browser_call", "browser_hand",
    "browser_input", "browser_secret", "browser_steps", "canvas_answer", "canvas_comment", "canvas_comments",
    "canvas_discard", "canvas_edit", "canvas_kit", "canvas_list", "canvas_open", "canvas_publish", "canvas_put",
    "canvas_accept", "canvas_edit_html", "canvas_reject", "card_share", "canvas_reply", "canvas_resolve", "canvas_section", "canvas_send", "canvas_show", "claim", "decline",
    "decline_message", "dep_add", "dep_remove", "deploy_now", "dispatch_item", "done", "drop", "give", "goal_add",
    "goal_done", "goal_own", "goal_release", "item_add", "item_edit", "item_reply", "launch_agent", "message_read", "move",
    "next_claim", "offer", "open_agent_on", "open_needs_you", "ops_add", "ops_beat", "ops_pause", "ops_repair",
    "ops_run", "ops_trigger_run", "ops_trial", "pending_deliver", "pending_send_now", "prio", "push", "push_cancel", "queue_remove",
    "register", "release", "release_check", "reopen", "replanned", "restart", "review_mark", "review_record",
    "review_send",
    "run_hooks", "secret", "send", "serve_restart", "serve_status", "split", "start_session", "unblock",
    "usage_read",  # #6387: a reading of an agent's usage, in a hidden window of its own
    "pipeline_fix", "pipeline_correct",
    "pipeline_operators", "pipeline_emergency",  # #6145: the functions check a person, or the pipeline's operators
    "pipeline_run_stage",  # #6138: the same
    "pipeline_start_kind",  # #6603: as maxpm pipeline start; deps.start and target_cut check who starts it
    "pipeline_definition_back")}  # #6143: versions.restore takes a person or the manager
OPS.update({op: LEAD for op in (
    "agent_interrupt", "session_kill_command", "prompt_approve", "relay", "release_summary", "config_set", "config_unset", "expert_end", "expert_fresh", "goal_edit", "goal_rank",
    "goal_reopen", "kill_agent", "ops_accept", "ops_reject", "policy_add", "policy_edit", "policy_retire",
    "project_add", "project_archive", "project_describe", "project_rank", "project_restore", "queue_add",
    "start_expert", "stop_agent", "target_cadence", "target_cut", "target_hook", "tmux_tidy", "folder_add", "pause",
    "resume", "pipeline_hold", "pipeline_unhold", "pipeline_approve", "pipeline_rerun", "pipeline_rerun_failed",
    "pipeline_rollback", "pipeline_stop", "pipeline_release", "pipeline_transition", "pipeline_block", "pipeline_unblock",
    "pipeline_delay", "pipeline_disable", "pipeline_enable", "pipeline_pass", "pipeline_freeze",
    "pipeline_account_check", "pipeline_move_back", "pipeline_trigger",
    "runner_add", "runner_check", "runner_allow", "runner_stop",
    # #6430: an update changes the agent CLI of every session on this computer; a person or the manager decides it
    "agent_update", "agent_update_cancel", "agent_update_later", "agent_update_auto", "agent_update_check")})
OPS.update({op: ONLY_PERSON for op in (
    "pipeline_token_add", "pipeline_token_make",  # #6148: a person saves a forge token, or asks a session to make it
    "device_remove",  # #6471: Remove a paired phone in Settings
    "dictate_audio", "dictate_start", "dictate_stop", "host_setup", "host_signin", "prompt_answer", "relay_connect",
    "relay_disconnect", "release_changes", "release_download", "release_now", "release_agent", "release_update", "rqs_connect", "session_interrupt",
    "session_pin", "session_restart", "session_send", "session_setting", "session_upload",
    "session_upload_drop", "setup_agent_add", "setup_block", "setup_desktop", "setup_maxpm_cmd", "setup_ntfy",
    "setup_skills",
    "setup_tmux", "setup_agent_install", "setup_agent_signin",
    "shell_close", "shell_keys", "shell_open", "start_manager", "tailnet_serve", "takeover_seen", "takeovers_seen",
    "terminal_keys", "undo_takeover", "update")})

REFUSED = "refused: not allowed"  # the first words of a refusal of this table (the run log counts them)
REPEAT = 3  # refusals of one session within REPEAT_WINDOW that alert the manager
REPEAT_WINDOW_MIN = 10


def command_key(args):
    """'goal own' for maxpm goal own, 'list' for maxpm list: the key of the table and of READS."""
    subs = [getattr(args, k) for k in sorted(vars(args)) if k.endswith("cmd") and k != "cmd"
            and isinstance(getattr(args, k), str)]
    return " ".join([args.cmd, *subs[:1]])


def callers_of(key):
    """The callers that may run a command key ('goal own')."""
    cmd, _, sub = key.partition(" ")
    entry = COMMANDS.get(cmd)
    if entry is None:
        return None
    if isinstance(entry, dict):
        return entry.get(sub) or entry.get("*") or ANY
    return entry


def refusal(key, caller, name=None):
    """Why this caller may not run the command, or None. caller: person, manager, session, or None (inside an agent
    with no name)."""
    allowed = callers_of(key)
    if allowed is None:
        return f"{REFUSED}: maxpm {key} is in no permission table (river/rights.py)"
    if caller is None:
        if key in READS:
            return None
        return (f"{REFUSED}: this command runs inside an agent session with no name, so it may only read. The session "
                f"that maxpm serve started has its token; else name it: maxpm --as <session> {key} ...")
    if caller in allowed:
        return None
    who = {PERSON: "a person", MANAGER: "the manager", SESSION: f"the session {name}" if name else "a session"}[caller]
    may = " or ".join({PERSON: "a person", MANAGER: "the manager", SESSION: "a session"}[c]
                      for c in (PERSON, MANAGER, SESSION) if c in allowed)
    return (f"{REFUSED}: maxpm {key} is for {may}, and {who} runs it. "
            + ("Ask the manager: maxpm alert <manager> \"<what you need>\" --blocked" if caller == SESSION else
               "The person does it on the page" if allowed == ONLY_PERSON else ""))


def op_refusal(op, caller, name=None):
    # Every action of server.OPS is in OPS (a test proves it); an action that is not is one a test added for itself.
    allowed = OPS.get(op, ANY)
    if caller in allowed:
        return None
    return (f"{REFUSED}: the action {op} is for " + " or ".join(
        {PERSON: "a person", MANAGER: "the manager", SESSION: "a session"}[c] for c in (PERSON, MANAGER, SESSION)
        if c in allowed) + f", and {name or 'this caller'} asks for it")
