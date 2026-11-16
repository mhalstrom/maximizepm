"""The blocks of a Markdown text for Edit in place (river/static/components/mdBlocks.js, #6004), run in node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "river" / "static"
TEXT = "# Plan\n\nShip in March.\nAsk the team.\n\n- one\n- two\n\n```js\na = 1\n\nb = 2\n```\n## Risks\nNone yet."


def run(expr):
    js = f"const m = await import('./components/mdBlocks.js'); const T = {json.dumps(TEXT)}; console.log(JSON.stringify({expr}));"
    out = subprocess.run(["node", "--input-type=module", "-e", js], cwd=STATIC, capture_output=True, text=True,
                         timeout=30, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not on this machine")
class MdBlocks(unittest.TestCase):
    def test_blocks(self):
        b = run("m.mdBlocks(T).map(x => x.src)")
        self.assertEqual(b, ["# Plan", "Ship in March.\nAsk the team.", "- one\n- two", "```js\na = 1\n\nb = 2\n```",
                             "## Risks", "None yet."])

    def test_splice_keeps_the_rest_and_delete_takes_the_blank_line(self):
        self.assertEqual(run("m.spliceBlock(T, m.mdBlocks(T)[1], 'Ship in May.')"),
                         TEXT.replace("Ship in March.\nAsk the team.", "Ship in May."))
        self.assertEqual(run("m.spliceBlock(T, m.mdBlocks(T)[2], '')"), TEXT.replace("- one\n- two\n\n", ""))


if __name__ == "__main__":
    unittest.main()
