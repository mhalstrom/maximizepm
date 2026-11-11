# Gemini CLI fixtures (#5174)

For the tests of `river/agents/gemini.py`: Gemini CLI 0.68.0 (`@google/gemini-cli`).

- `session-*.jsonl`: chat logs in the JSONL form that Gemini writes to
  `~/.gemini/tmp/<project>/chats/session-<YYYY-MM-DDTHH-MM>-<first 8 of the id>.jsonl`. The record shapes come from a
  real log of Gemini CLI 0.64 on the maintainer's Mac (the metadata line, user, info and gemini lines, a gemini line
  written twice with the same id, tool calls with their results, the tool results as a user line, `$set` lines) and
  from the 0.68.0 source of the chat recording service (`displayContent`, `$rewindTo`, `$patch`, the tool call status,
  the error line, the summary). The text is neutral: the real log named another project.
- `pane_*.txt`: the screens of a pane: idle (empty and with a draft), working, and blocked (a shell command, a file
  edit, folder trust, sign-in). Their text comes from the UI source of 0.68.0 (InputPrompt, LoadingIndicator,
  ToolConfirmationMessage, FolderTrustDialog, AuthDialog). Record them again from a running Gemini CLI when one is
  installed and signed in on this Mac (the follow-up item of #5174).
