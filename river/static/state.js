// The page's copy of the state, kept current with small answers (#4709). The page sends the version it has
// (api/state?since=<v>), and maxpm serve answers with only what changed (river/statedelta.py): whole parts in
// "parts", and for lists of rows with a key, the new and changed rows ("set"), the keys that left ("del"), and the
// new order ("order", only when it changed). An answer with "state" replaces the copy.
export const KEYED = { items: "id", agents: "name", takeovers: "id", needs_you: "id", events: "id" };

// The new state from the old one and an answer. The old state is left as it was.
export function applyAnswer(old, ans) {
  if (ans.state || !old) return ans.state;
  const next = { ...old, ...(ans.parts || {}) };
  for (const name of ans.drop || []) delete next[name];
  for (const [name, ch] of Object.entries(ans.rows || {})) {
    const key = ch.key || KEYED[name], byKey = new Map((old[name] || []).map(r => [r[key], r]));
    for (const k of ch.del || []) byKey.delete(k);
    for (const r of ch.set || []) byKey.set(r[key], r);
    if (ch.order) next[name] = ch.order.map(k => byKey.get(k));
    else {
      // The same order: the old rows in place, changed ones replaced. A new row always comes with an order.
      const gone = new Set(ch.del || []);
      next[name] = (old[name] || []).filter(r => !gone.has(r[key])).map(r => byKey.get(r[key]));
    }
  }
  return next;
}

// Keeps the version, and asks one at a time: two answers to the same version must not both apply.
export function makeStateClient(fetchJson) {
  let v = null, cur = null, chain = Promise.resolve();
  function load(closed) {
    const run = async () => {
      const q = new URLSearchParams({ since: v || "" }); if (closed) q.set("closed", "1");
      const ans = await fetchJson("api/state?" + q);
      cur = applyAnswer(cur, ans); v = ans.v;
      return cur;
    };
    const p = chain.then(run, run); chain = p.catch(() => {});
    return p;
  }
  return { load, version: () => v };
}
