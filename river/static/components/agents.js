// The registered agents (#5184): each agent's describe part, the parts it supports, its launch capabilities and its
// setup texts (the label and tooltip of its web link, why a session cannot open, its voice), from GET /api/agents,
// read once. A view asks this part about an agent and tests no agent name itself (river/agents, docs/agent-adapters.md).
let list = [];
let loading = null;

// Read the agents once; each later call gives the same promise. A failed read is tried again at the next call.
export function loadAgents() {
  if (!loading) {
    loading = fetch("api/agents").then(r => r.ok ? r.json() : Promise.reject(new Error(r.statusText)))
      .then(j => { list = j.agents || []; return list; })
      .catch(() => { loading = null; return list; });
  }
  return loading;
}

// The agent of a session's provider ({id, agent}) or of a name (its id or an alias, any case); null before the
// read, or for an agent that is not registered.
export function agentOf(p) {
  if (!p) return null;
  const n = String(typeof p === "string" ? p : (p.agent || p.id || "")).toLowerCase();
  return list.find(a => (a.names || [a.id]).includes(n)) || null;
}

// The first registered agent (Claude Code): its sessions carry no agent tag on the Sessions tab.
export const mainAgent = () => list.find(a => a.main) || null;

// The setup texts of an agent ({} when there are none): open_link, open_title, no_link, voice, instructions.
export const setupOf = (p) => ((agentOf(p) || {}).setup) || {};
