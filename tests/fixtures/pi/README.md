Pi session files for tests/test_agent_pi.py (#5170, #5204).

session.jsonl and fork.jsonl are files that a real Pi 1.5.2 session wrote (npm package @earendil-works/pi-coding-agent),
recorded on 2026-11-11 for #5204. A script drove `pi --mode rpc --provider mistral --model mistral-small-latest
--thinking high` with the state extension of river/agents/pi.py (extension_text, loaded with -e) and one more
test extension, which adds a /goto <entry id> command that calls ctx.navigateTree(id, {summarize: true}), as /tree
does. The steps:

1. A prompt with a 1x1 PNG image that asks for `echo hello` with the bash tool: a tool call and its result.
2. One more prompt, then /goto back to the first answer: the answer stays on a branch that /tree left, and a
   branch_summary entry takes its place.
3. A prompt that makes the model read missing.md: a read tool call with an error result.
4. compact (with compaction.keepRecentTokens 200 in the test agent folder's settings.json, so the small session
   compacts).
5. bash `echo nothing to commit` (the RPC form of a !command): a bashExecution message.
6. set_thinking_level off (Mistral Small has only off and high), then a prompt.
7. fork from the last user message, then a prompt: fork.jsonl.

The changes from the recorded files: the session folder is /work/demo, and fork.jsonl's parentSession is PARENT
(the test puts the path of its copy of session.jsonl there). Pi read no instruction file into the system message: the
session ran in a folder with no CLAUDE.md or AGENTS.md above it.

What the recording showed:

- The state extension reports idle at the start, then working and idle around each run, in the order of --seq (the
  calls run at the same time, so they can reach maxpm out of order; --seq sorts them). A quit (stdin closed) runs
  maxpm release.
- A fork or clone file holds a copy of its branch from the root, with the same entry ids. chat_chain therefore stops
  at a fork: its parent would show the same messages twice.
- Pi 1.5.2 writes a system message with the sections of the system prompt at the start of a session, and two
  session_info entries when the extension names the session and a person names it again.
