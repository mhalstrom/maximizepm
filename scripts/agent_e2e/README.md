# End-to-end test of the agent programs (#6399)

One run of each agent program on a queue of its own. The results are in `docs/agent-adapters.md`, section
"End-to-end run of every agent (#6399)".

Run everything outside an agent sandbox: the test serve binds a port, and the agents use the person's sign-ins.

```
T=$(mktemp -d)                                   # the test queue and its folders
bash scripts/agent_e2e/q.sh $T init
bash scripts/agent_e2e/q.sh $T config set serve_port 18931
bash scripts/agent_e2e/q.sh $T config set claude_remote_control off
bash scripts/agent_e2e/q.sh $T config set result_look push_first
bash scripts/agent_e2e/q.sh $T serve --port 18931 > $T/serve.log 2>&1 &   # its own tmux server (TMUX_TMPDIR=$T)
item=$(bash scripts/agent_e2e/prep.sh $T pi)    # add 'worktree' for a MaximizePM worktree
python3 scripts/agent_e2e/drive.py $T Pi $item - low
bash scripts/agent_e2e/inst.sh $T/inst          # AGENTS.md in print mode
```

- `q.sh`: maxpm of this checkout on the test queue, with `env -i`: no `MAXPM_TOKEN`, `MAXPM_AGENT` or Claude Code
  variable of the calling session reaches it.
- `tx.sh`: tmux of the test serve. It clears `TMUX`, or tmux reaches the person's server.
- `drive.py`: launch, maxpm go, claim, done, wait, a question, stop. It answers no card of the agent: a step that
  waits at a trust or approval question fails with the question's text. `OPTIONS="force=on"` before it adds launch
  settings (`maxpm launch --option`), as a person sets them in the launch dialog.
- `inst.sh`: each agent in print mode, asked for the codewords of two `AGENTS.md` files.

End the run: stop each session (`maxpm stop`), stop the test serve, then `bash scripts/agent_e2e/tx.sh $T
kill-session -t maxpm`.
