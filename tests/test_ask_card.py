"""The card of a question or a person item (river/static/components/askCard.js, #6002), run in node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "river" / "static"


def run(expr):
    js = f"const m = await import('./components/askCard.js'); console.log(JSON.stringify({expr}));"
    out = subprocess.run(["node", "--input-type=module", "-e", js], cwd=STATIC, capture_output=True, text=True,
                         timeout=30, check=True)
    return json.loads(out.stdout)


QUESTION = ("{kind: 'message', message_id: 7, message_kind: 'question', item_id: 12, type: 'decide', from_agent: 'w1',"
            " message_level: null, message_blocked: 'w1', opened_at: '2026-11-16T00:00:00Z',"
            " body: 'Decision 1 of 1: scopes\\nYour answer: \"A\" or \"B\"'}")


@unittest.skipUnless(shutil.which("node"), "node is not on this machine")
class AskCard(unittest.TestCase):
    def test_a_question_card_has_its_tag_level_options_and_answer(self):
        html = run(f"m.cardHtml(m.fromEvent({QUESTION}))")
        self.assertIn('data-kind="question"', html)
        self.assertIn("c-type-decide", html)
        self.assertIn("High", html)
        self.assertIn("its session waits", html)
        self.assertIn('data-opt="A"', html)
        self.assertIn('data-opt="B"', html)
        self.assertIn("Open the item", html)  # its item #12
        self.assertIn("Discuss with the manager", html)
        self.assertIn(">Answer<", html)

    def test_a_person_item_has_the_same_card_with_done(self):
        html = run("m.cardHtml(m.fromItem({id: 9, title: 'Sign the form', context: 'In the drawer', type: 'do', "
                   "priority: 1, project: 'p'}))")
        self.assertIn('data-kind="item"', html)
        self.assertIn("c-type-do", html)
        self.assertIn(">P1<", html)
        self.assertIn("In the drawer", html)
        self.assertIn(">Done<", html)
        self.assertNotIn("data-opt", html)  # no decision form, no option buttons

    def test_a_look_card_of_tries_shows_them_side_by_side(self):
        # #5996: each try with what its session said to look at; a button keeps a done try.
        html = run("m.cardHtml(m.fromItem({id: 20, title: 'Look at the 2 tries and pick one: x', type: 'look', "
                   "context: 'Your answer: \"1\" or \"2\"', tries: [{k: 1, id: 21, status: 'done', output: 'canvas h1', "
                   "assignee: 's1'}, {k: 2, id: 22, status: 'in_progress', output: null}]}))")
        self.assertIn("Try 1", html)
        self.assertIn("canvas h1", html)
        self.assertIn('data-opt="1"', html)
        self.assertNotIn('data-opt="2"', html)  # not done yet
        self.assertIn("Not done yet (in progress)", html)

    def test_text_is_escaped(self):
        html = run("m.cardHtml(m.fromItem({id: 9, title: '<img src=x onerror=alert(1)>', type: 'do'}))")
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)

    def test_the_side_column_and_discuss_text(self):
        side = run("m.sideHtml({comments: [{from_agent: 'maxpm', kind: 'note', body: 'hi', created_at: '2026-11-16T00:00:00Z'}],"
                   " history: [{at: '2026-11-16T00:00:00Z', change: 'added'}]})")
        self.assertIn("MaximizePM", side)
        self.assertIn("added", side)
        self.assertIn("Reading", run("m.sideHtml(null)"))
        text = run(f"m.discussText(m.fromEvent({QUESTION}))")
        self.assertIn("question #7 from w1 (decide)", text)


if __name__ == "__main__":
    unittest.main()
