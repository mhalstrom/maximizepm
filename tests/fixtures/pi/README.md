Pi session files for tests/test_agent_pi.py (#5170).

session.jsonl and fork.jsonl follow Pi 0.73.1's session format (docs/session-format.md of the npm package
@mariozechner/pi-coding-agent): the entry types, the field names and their order. They were written by hand from
that format, because the Pi CLI was not installed on this Mac when the adapter was written. Replace them with files
that a real Pi session records (the follow-up item of #5170).
