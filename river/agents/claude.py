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
