"""The tier 1 agents (#6252, docs/agent-adapters.md, section 'Tier 1 agents'): each agent of agents.TIER1 has every
feature of the section that code can check. A failure names the agent and the feature. A feature that an agent lacks
is in EXCEPTIONS, with its fix item or the reason it cannot have it, and the section names it too; an exception
that no longer holds fails, so the list stays true. The features that only a real run proves are the columns of the
section's end-to-end table (#6253)."""

import os
import re
import tempfile
import unittest
from pathlib import Path

from river import agentinstall, agentlogin, agents, core, runners, vault
from river.pipeline import agent_job

DOC = Path(__file__).resolve().parent.parent / "docs" / "agent-adapters.md"


def _argv(words):
    return isinstance(words, list) and bool(words) and all(isinstance(w, str) for w in words if w is not None)


def _exe_in(a, words):
    return any(Path(str(w).split()[0]).name == a.describe["exe"] for w in words if w)


def _parts(a):
    gone = [p for p in agents.PARTS if not a.supports(p)]
    return f"lacks the adapter parts {', '.join(gone)}" if gone else None


def _launch(a):
    argv = a.build_command("tier1-test", "/tmp/tier1", None, None, {}, "hello")
    return None if _argv(argv) and _exe_in(a, argv) else f"build_command gives {argv!r}, not a start of its program"


def _resume(a):
    argv = a.resume_command("tier1abc", None, None, {})
    if not _argv(argv) or not _exe_in(a, argv) or not any("tier1abc" in str(w) for w in argv if w):
        return f"resume_command gives {argv!r}, not a resume of session tier1abc"
    return None


def _identity(a):
    i = a.identity or {}
    if i.get("source") not in agents.IDENTITY_SOURCES or not i.get("inside_env"):
        return "IDENTITY needs a source and the variables that tell a command runs inside it (inside_env)"
    return None


def _chat(a):
    c = a.chat or {}
    if c.get("ref") not in agents.CHAT_REFS or not a.has("chat_path") or not a.has("parse_chat"):
        return "CHAT needs a ref, chat_path() and parse_chat(): the Sessions tab shows no chat without them"
    return None


def _states(a):
    d = a.layout()
    if (a.state or {}).get("state") not in agents.STATE_SOURCES:
        return "STATE needs one source of the state: hook or detect"
    if not d.get("idle") or not d.get("working") or not d.get("blocked"):
        return "DETECT needs idle, working and blocked rules (the pane watcher reads blocked screens of every agent)"
    if not any(re.search(r"(?i)trust", b) for b in d["blocked"]):
        return "no DETECT['blocked'] rule reads the folder trust question"
    return None


def _ask(a):
    d = a.layout()
    if not d.get("ask") or d.get("ask_form") not in agents.ASK_FORMS:
        return "DETECT needs 'ask' (the first line of its question dialog) and an 'ask_form' that river/ask_forms knows"
    return None


def _interrupt(a):
    return None if a.input.get("interrupt") else "INPUT['interrupt'] names no key that ends a turn (Stop on the page)"


def _native(a):
    return None if a.input.get("native") else "INPUT['native'] names no command that delivers a message mid-turn"


def _commands(a):
    if not a.commands or not a.has("read_commands"):
        return "needs COMMANDS and read_commands(folder) for the '/' list"
    if not any(c.get("setting") == "model" for c in a.commands):
        return "no command opens the Settings dialog at Model (a COMMANDS entry with setting 'model')"
    return None


def _effort(a):
    e = a.effort or {}
    if not (e.get("levels") or a.has("read_effort_levels")) or not e.get("flag"):
        return "EFFORT needs its levels (or read_effort_levels) and the flag that sets one"
    return None


_DB = {}


def _model_order(a):
    """#6256: the launch dialog's model list is in the one order (newest and strongest first), each model once, and
    its effort list has only the levels the agent and the model take, low to high."""
    from tests.test_model_order import order_problem
    if "c" not in _DB:
        d = tempfile.TemporaryDirectory()
        _DB.update(dir=d, c=core.connect(os.path.join(d.name, "t.db")))
    ms = core.platform_models(_DB["c"], core._platform_of(a))
    if not ms:
        return "it offers no model"
    problem = order_problem(ms, a.describe.get("tiers") or ())
    if problem:
        return f"the model list breaks the order: {problem}"
    if len({m["name"] for m in ms}) != len(ms):
        return "a model shows twice in the model list"
    ch = a.effort_choices()
    if len({a.effort_name(lv) for lv in ch}) != len(ch):
        return "an effort level of the agent shows twice in the effort list"
    return None


def _session_settings(a):
    """#6260: a running session's Settings dialog reads the model and the effort it runs (and its mode) from the
    session itself, its chat log (chat_settings) or its screen (screen_settings), not only from a command line that
    may name none."""
    if not (a.has("chat_settings") or a.has("screen_settings")):
        return "neither chat_settings nor screen_settings: a session started with no --model shows 'not known'"
    keys = [s["key"] for s in a.session_specs()]
    if "model" not in keys or "effort" not in keys:
        return f"the Settings dialog has no model or effort row (it has {', '.join(keys)})"
    return None


def _usage(a):
    if not a.has("context_window") or not (a.has("chat_usage") or a.has("chat_requests")):
        return "needs context_window() and chat_usage() or chat_requests() for the Usage panel and maxpm usage"
    return None


def _setup_files(a):
    s = a.setup or {}
    if not s.get("instructions") or not a.has("write_mcp_config"):
        return "SETUP needs its instruction file and write_mcp_config() (MaximizePM's MCP server)"
    return None


def _setup_main(a):
    m = agentinstall.METHODS.get(core._platform_of(a)) or {}
    gone = [k for k in ("install", "signin", "status") if not m.get(k)]
    return f"agentinstall.METHODS has no {', '.join(gone)} for it" if gone else None


def _host_signin(a):
    return None if a.describe["exe"] in agentlogin.AGENTS else "agentlogin.AGENTS has no sign-in for it"


def _roles(a):
    if core._agent_platform(a.id) is None:
        return "agent_for.<role> and default_agent refuse it (core._agent_platform)"
    return None


def _jobs(a):
    exe = a.describe["exe"]
    gone = [t for t, have in (("runners.CLIS", runners.CLIS), ("agent_job.SECRET", agent_job.SECRET)) if exe not in have]
    return f"{' and '.join(gone)} have no {exe}: no --cli {exe} for a runner or a CI job" if gone else None


def _shell_guard(a):
    if not a.has("install_hooks") or not a.has("read_hook"):
        return "no hook before a shell command (install_hooks and read_hook): tmux_guard and push_guard never run"
    return None


def _secrets(a):
    p = a.layout().get("process")
    return None if p in vault.AGENT_PROGRAMS else f"vault.AGENT_PROGRAMS has no {p!r}: maxpm secret refuses it"


# The agents that run a shell command with no folder of its own in a folder that --add-dir names, not in their start
# folder (measured: agy 1.5.13 in a chat, #6278). Claude Code and Codex run it in their start folder: the end-to-end
# run proves that.
RUNS_IN_ADDED = {"antigravity"}


def _first_go(a):
    argv = a.build_command("tier1-test", "/tmp/tier1", None, None, {}, "maxpm go")
    if a.id in RUNS_IN_ADDED and "--add-dir" in argv:
        return "its start names --add-dir, so its first maxpm go may run there, not in the project folder"
    return None


def _read_dirs(a):
    """#6281: the adapter takes the project's read_dirs, and its start refuses the secret file of a read folder."""
    if not a.launch.get("read_dirs"):
        return "LAUNCH has no read_dirs: the project's read folders do not reach its start"
    argv = a.build_command("tier1-test", "/tmp/tier1", None, None,
                           {"read_dirs": ["/tmp/tier1-read"], "secret_files": ["/tmp/tier1-read/.env"]}, "maxpm go")
    text = " ".join(str(w) for w in argv if w)
    if not re.search(r"tier1-read[^ ]*\.env", text):
        return "its start names no rule that refuses the read folder's .env"
    return None


# feature -> its check: a problem text, or None. The keys are the feature names of the doc's section.
FEATURES = {
    "parts": _parts, "launch": _launch, "resume": _resume, "identity": _identity, "chat": _chat,
    "states": _states, "ask_card": _ask, "interrupt": _interrupt, "native_message": _native,
    "commands": _commands, "effort": _effort, "model_order": _model_order, "session_settings": _session_settings, "usage": _usage, "setup_files": _setup_files,
    "setup_main": _setup_main, "host_signin": _host_signin, "roles": _roles, "jobs": _jobs,
    "shell_guard": _shell_guard, "secrets": _secrets, "first_go": _first_go,
    "read_dirs": _read_dirs,
}

# (agent, feature) -> why it lacks the feature: its fix item, or why it cannot have it. The doc's section lists each.
EXCEPTIONS = {
    ("antigravity", "native_message"): "no command, socket or flag",
    ("antigravity", "shell_guard"): "push_guard reads the queue's state",
    ("codex", "read_dirs"): "its sandbox has no rule that refuses one file",
}


def _section():
    text = DOC.read_text()
    m = re.search(r"^## Tier 1 agents\n(.*?)(?=^## )", text, re.S | re.M)
    return m.group(1) if m else ""


class Tier1(unittest.TestCase):

    def test_each_tier1_agent_is_registered(self):
        for aid in agents.TIER1:
            with self.subTest(agent=aid):
                self.assertIsNotNone(agents.find(aid), f"tier 1 agent {aid} has no adapter")
                self.assertTrue(agents.is_tier1(aid))

    def test_each_feature(self):
        for aid in agents.TIER1:
            a = agents.find(aid)
            if a is None:
                continue  # test_each_tier1_agent_is_registered fails for it
            for name, check in FEATURES.items():
                with self.subTest(agent=aid, feature=name):
                    problem = check(a)
                    why = EXCEPTIONS.get((aid, name))
                    if why:
                        self.assertIsNotNone(problem, f"{aid} has feature {name} now: take ({aid!r}, {name!r}) out "
                                                      f"of EXCEPTIONS and out of the doc's Tier 1 section")
                    else:
                        self.assertIsNone(problem, f"tier 1 agent {aid} lacks feature {name}: {problem}")

    def test_the_doc_names_each_feature_and_exception(self):
        sec = _section()
        self.assertTrue(sec, "docs/agent-adapters.md has no '## Tier 1 agents' section")
        for name in FEATURES:
            self.assertIn(f"`{name}`", sec, f"the Tier 1 section does not name feature {name}")
        for (aid, name), why in EXCEPTIONS.items():
            line = next((x for x in sec.splitlines() if f"`{name}`" in x and why in x), None)
            self.assertIsNotNone(line, f"the Tier 1 section does not state the exception {aid} {name} ({why})")
        label = {a: agents.find(a).label for a in agents.TIER1 if agents.find(a)}
        for aid, lab in label.items():
            self.assertIn(lab, sec, f"the Tier 1 section does not name {lab}")

    def test_a_non_tier1_agent_is_not_main(self):
        self.assertFalse(agents.is_tier1("aider"))
        self.assertTrue(agents.is_tier1("claude-code"))  # a platform name, as the setup guide passes it
        self.assertFalse(agents.is_tier1(""))


if __name__ == "__main__":
    unittest.main()
