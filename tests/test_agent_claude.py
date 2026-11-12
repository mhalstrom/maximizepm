"""The Claude Code adapter's chat reader, river/agents/claude.py, against the lines in tests/fixtures/claude."""
import unittest
from pathlib import Path

from river.agents import claude

FIX = Path(__file__).resolve().parent / "fixtures" / "claude"


def lines(name):
    return (FIX / name).read_text().splitlines()


class QueuedMidTurn(unittest.TestCase):
    """#5297: a message that the person sends while the session works shows in the chat, once, at its time."""

    def setUp(self):
        self.entries, self.turn = claude.parse_chat(lines("queued_mid_turn.jsonl"))
        self.users = [e for e in self.entries if e["role"] == "user"]

    def test_each_message_of_the_person_is_one_user_turn(self):
        self.assertEqual([e["text"] for e in self.users],
                         ["also check the tests of the header", "[Image #1] the header looks like this now"])

    def test_turn_has_its_time_and_image(self):
        self.assertEqual(self.users[0]["at"], "2026-11-11T08:00:04.900Z")
        self.assertNotIn("image_count", self.users[0])
        self.assertEqual(self.users[1]["image_count"], 1)

    def test_the_turn_comes_after_the_tool_result_it_came_with(self):
        self.assertEqual([e["role"] for e in self.entries], ["tool", "result", "user", "tool", "result", "user"])

    def test_other_sessions_and_task_notices_are_no_turn_of_the_person(self):
        texts = " ".join(e.get("text", "") for e in self.entries)
        self.assertNotIn("cross-session-message", texts)
        self.assertNotIn("task-notification", texts)

    def test_a_queued_line_does_not_change_the_turn_state(self):
        for line in lines("queued_mid_turn.jsonl"):
            if '"queued_command"' in line:
                self.assertIsNone(claude.parse_chat([line])[1])

    def test_a_shell_command_an_empty_prompt_or_a_subagent_is_skipped(self):
        d = {"type": "attachment", "isSidechain": False, "timestamp": "2026-11-11T08:00:00Z",
             "attachment": {"type": "queued_command", "origin": {"kind": "human"}, "commandMode": "bash", "prompt": "ls"}}
        self.assertEqual(claude.parse_chat_line(d), ([], None))
        d["attachment"].update(commandMode="prompt", prompt="  ")
        self.assertEqual(claude.parse_chat_line(d), ([], None))
        d["attachment"]["prompt"] = "hello"
        self.assertEqual(claude.parse_chat_line(d)[0][0]["text"], "hello")
        d["isSidechain"] = True
        self.assertEqual(claude.parse_chat_line(d), ([], None))


if __name__ == "__main__":
    unittest.main()


class Interrupted(unittest.TestCase):
    """#5547: Esc in its terminal (or Stop on the Sessions tab) ends the turn. Claude Code writes a user line for it:
    the chat shows 'stopped', no turn of the person, and the session is idle."""

    def line(self, text):
        return {"type": "user", "isSidechain": False, "timestamp": "2026-11-12T08:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]}}

    def test_an_interrupt_is_an_event_and_ends_the_turn(self):
        for text in ("[Request interrupted by user]", "[Request interrupted by user for tool use]"):
            out, turn = claude.parse_chat_line(self.line(text))
            self.assertEqual(([(e["role"], e["text"]) for e in out], turn), ([("event", "stopped")], "idle"))

    def test_a_prompt_in_brackets_is_still_the_persons_turn(self):
        out, turn = claude.parse_chat_line(self.line("[draft] look at the header"))
        self.assertEqual(([e["role"] for e in out], turn), (["user"], "working"))
