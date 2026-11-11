"""The Claude Code adapter (docs/agent-adapters.md). Only its describe part so far: #5181 moves launch, resume and
identity here, #5182 the chat log, #5183 the state (its hooks), #5184 the setup, #5169 the settings and effort."""

DESCRIBE = {
    "id": "claude",
    "label": "Claude Code",
    "exe": "claude",
    "aliases": ["claude-code"],  # the platform name in default_agent, agent_rules, --agent and launch_agents
    "family": "claude",
    # claude --model takes these names as they are; claude_model_ids maps a name to a full id when a person wants one.
    "models": ["haiku", "sonnet", "opus", "fable"],
    "default_model": "",
    "model_ids": {},
    "prefix": "claude_",
}


# ---------------------------------------------------------------------------------------------------------------
# Effort, launch, identity and resume (#5181)
# ---------------------------------------------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from . import Raw  # noqa: E402

EFFORT = {
    "levels": ["low", "medium", "high", "xhigh", "max"],
    "map": {},
    "flag": ["--effort", "{value}"],
    "live": "/effort {value}",
    "read": r"--effort ([a-z][a-z0-9_-]{0,31})\b",
}

LAUNCH = {"session_name": True, "system_prompt_file": True, "hooks_file": True, "fork": True, "resume": True,
          "autocompact": True, "background": True}

IDENTITY = {"source": "env", "env": "CLAUDE_CODE_SESSION_ID", "log_glob": None, "pane": None, "url": None,
            "inside_env": ["CLAUDECODE"]}


def _q(s):
    from ..core import _shell_quote
    return _shell_quote(s)


def build_command(name, folder, model, effort, settings, prompt):
    """claude [--name N] [--model M] [--effort E] [--permission-mode P] <args> <prompt> [--remote-control N]. The args
    setting goes in as the person wrote it, after what MaximizePM puts before it, in this order: --autocompact,
    --settings (the hooks file), then --resume <id> (a resume) or --resume <base> --fork-session (a fork) after the
    skill's --append-system-prompt-file. The prompt goes before --remote-control: that flag takes an optional session
    name after it. A full model id can end in [1m], a glob in zsh (#4950): every word but the args is quoted."""
    from ..core import skill_prompt_args
    s = settings
    args = s.get("args", "") or ""
    if s.get("resume"):
        args = f"--resume {_q(s['resume'])} " + args
    else:
        if s.get("system_prompt_file"):
            args = skill_prompt_args(args)
        if s.get("fork"):
            args = f"--resume {_q(s['fork'])} --fork-session " + args
    if s.get("hooks_file") and "--settings" not in args:
        args = f"--settings {_q(s['hooks_file'])} " + args
    if s.get("autocompact") and "--autocompact" not in args:
        args = f"--autocompact {s['autocompact']} " + args
    words = ["claude"] + (["--name", name] if name else [])
    words += (["--model", model] if model else []) + ([w.format(value=effort) for w in EFFORT["flag"]] if effort else [])
    words += ["--permission-mode", s["permission_mode"]] if s.get("permission_mode") else []
    words += [Raw(args.strip())] if args.strip() else []
    words.append(prompt)
    words += ["--remote-control"] + ([name] if name else []) if s.get("remote_control") == "on" else []
    return words


def resume_command(session_ref, model, effort, settings, name=None):
    """claude --resume <session id>, with the profile's settings and settings['prompt'] as its first prompt."""
    return build_command(name, None, model, effort, {**settings, "resume": session_ref}, settings.get("prompt"))


def parse_command(words):
    """A hand-written claude command that a profile builds exactly: --model {model}, --effort {effort},
    --remote-control (with no session name after it), --permission-mode <a known mode>, and one prompt. Anything
    else (another flag, a fixed model) keeps it a custom command, so nothing is lost."""
    from ..core import LAUNCH_PLATFORMS
    if not words or Path(words[0]).name.lower() != "claude":
        return None
    modes = LAUNCH_PLATFORMS["claude-code"]["options"]["permission_mode"]["choices"]
    opts, prompt, i = {"remote_control": "off"}, None, 1
    while i < len(words):
        t, nxt = words[i], (words[i + 1] if i + 1 < len(words) else None)
        if t == "--model" and nxt == "{model}" or t == "--effort" and nxt == "{effort}":
            i += 2
        elif t == "--remote-control":
            if nxt is not None and not nxt.startswith("-"):
                return None  # a session name, or a prompt that the flag would take as one
            opts["remote_control"] = "on"
            i += 1
        elif t == "--permission-mode" and nxt in modes:
            opts["permission_mode"] = nxt
            i += 2
        elif not t.startswith("-") and prompt is None and t.strip() and ";" not in t:
            prompt = t
            i += 1
        else:
            return None
    return {"settings": opts, "prompt": prompt} if prompt is not None else None
