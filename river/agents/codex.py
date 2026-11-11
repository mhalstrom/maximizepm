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
