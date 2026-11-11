"""The Codex adapter (docs/agent-adapters.md). Only its describe part so far: #5181 moves launch, resume and
identity here, #5182 the chat log, #5183 the state, #5184 the setup, #5169 the settings and effort."""

DESCRIBE = {
    "id": "codex",
    "label": "Codex",
    "exe": "codex",
    "aliases": [],
    "family": "openai",
    "models": ["luna", "terra", "sol", "astra"],
    "default_model": "",
    # Codex takes full ids, and they change with each OpenAI release (the default of codex_model_ids, core.py).
    "model_ids": {"luna": "gpt-6-luna", "terra": "gpt-5.6-terra", "sol": "gpt-6.1-sol", "astra": "gpt-6-astra"},
    "prefix": "codex_",
}


def read_models():
    """The model ids in Codex's own model cache (~/.codex/models_cache.json, or $CODEX_HOME), or None without one."""
    from ..core import codex_models
    got = codex_models()
    return sorted(got) if got else None


# ---------------------------------------------------------------------------------------------------------------
# Effort, launch, identity and resume (#5181)
# ---------------------------------------------------------------------------------------------------------------

import os  # noqa: E402
from pathlib import Path  # noqa: E402

from . import Raw  # noqa: E402

EFFORT = {
    "levels": ["low", "medium", "high", "xhigh", "max"],  # when Codex's model cache does not list the model
    "map": {},
    "flag": ["-c", "model_reasoning_effort={value}"],
    "live": None,
    "read": r"model_reasoning_effort=([a-z][a-z0-9_-]{0,31})\b",
}

LAUNCH = {"extra_dirs": True, "resume": True}

# A Codex session records no task link: its web link is Codex's web app (core.CODEX_WEB).
IDENTITY = {"source": "env", "env": "CODEX_THREAD_ID", "log_glob": None, "pane": None, "url": "https://chatgpt.com/codex",
            "inside_env": ["CODEX_THREAD_ID", "CODEX_SANDBOX"]}


def read_effort_levels(model):
    """The effort levels Codex takes for model id `model`, from its model cache; None when the cache does not list it.
    MaximizePM refuses another effort for that model before it starts Codex."""
    from ..core import codex_models
    return (codex_models() or {}).get(model) if model else None


def build_command(name, folder, model, effort, settings, prompt):
    """codex [resume <id>] [-m M] [-c model_reasoning_effort=E] [--sandbox S] [--ask-for-approval A] --add-dir <queue
    folder> [--no-daemon] <args> <prompt>. Codex has no flag for a session name. Codex 0.159 ends at start, in a
    session with an API key, when it may use its shared background server ("requires api_key_model_discovery to be
    disabled"); --no-daemon runs it alone (#4621), when the installed Codex has the flag and the person did not take
    it out (settings['noflags'])."""
    from ..core import codex_flags, river_dir
    s = settings
    words = ["codex"] + (["resume", s["resume"]] if s.get("resume") else [])
    words += (["-m", model] if model else []) + ([w.format(value=effort) for w in EFFORT["flag"]] if effort else [])
    words += ["--sandbox", s["sandbox"]] if s.get("sandbox") else []
    words += ["--ask-for-approval", s["approval"]] if s.get("approval") else []
    words += ["--add-dir", river_dir()]
    if ("--no-daemon" in codex_flags() and "--no-daemon" not in s.get("args", "")
            and "--no-daemon" not in (s.get("noflags") or ())):
        words.append("--no-daemon")
    words += [Raw(s["args"].strip())] if s.get("args", "").strip() else []
    words.append(prompt)
    return words


def resume_command(session_ref, model, effort, settings, name=None):
    """codex resume <thread id>, with the profile's settings and settings['prompt'] as its first prompt."""
    return build_command(name, None, model, effort, {**settings, "resume": session_ref}, settings.get("prompt"))


def parse_command(words):
    """A hand-written codex command that a profile builds exactly: -m {model}, -c model_reasoning_effort={effort},
    --sandbox and --ask-for-approval with a known value, --add-dir of the queue folder, and one prompt."""
    from ..core import LAUNCH_PLATFORMS, river_dir
    if not words or Path(words[0]).name.lower() != "codex":
        return None
    choices = LAUNCH_PLATFORMS["codex"]["options"]
    opts, prompt, i = {}, None, 1
    while i < len(words):
        t, nxt = words[i], (words[i + 1] if i + 1 < len(words) else None)
        if t in ("-m", "--model") and nxt == "{model}" or t in ("-c", "--config") and nxt == "model_reasoning_effort={effort}":
            i += 2
        elif t in ("-s", "--sandbox") and nxt in choices["sandbox"]["choices"]:
            opts["sandbox"] = nxt
            i += 2
        elif t in ("-a", "--ask-for-approval") and nxt in choices["approval"]["choices"]:
            opts["approval"] = nxt
            i += 2
        elif t == "--add-dir" and nxt is not None and (
                nxt == "{maxpm_dir}" or os.path.realpath(os.path.expanduser(nxt)) == os.path.realpath(river_dir())):
            i += 2
        elif not t.startswith("-") and prompt is None and t.strip() and ";" not in t:
            prompt = t
            i += 1
        else:
            return None
    return {"settings": opts, "prompt": prompt} if prompt is not None else None


# ---------------------------------------------------------------------------------------------------------------

STATE = {"state": "detect", "session_id": "identity"}

# The words of its blocked screens: an approval ("Would you like to run the following command?"), the trust question,
# "Press enter to confirm or esc to cancel", a numbered menu with its marker (›). The same words as Claude Code's:
# the default of the prompt_pattern setting was one pattern for both.
BLOCKED = [r"Do you (want to|trust)", r"Would you like to", r"[Ee]nter to confirm", r"[❯›]\s*\d+\.\s"]

DETECT = {
    "process": "codex",
    "idle": r"^\s*›(\s|$)",  # the marker of its input box
    "working": r"(?i)\besc to interrupt\b",
    "blocked": BLOCKED,
    "tell_instead": r"(?i)\bno\b.*\btell\b",  # "No, and tell Codex what to do differently"
    "image_paste": None,  # not read yet: an image path is pasted alone, as for every agent
}

# Its native message command (the default of the native_message setting): codex queue adds a message to a thread.
INPUT = {"native": ["codex", "queue", "--thread", "{address}", "--message", "{text}"]}
