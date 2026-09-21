"""The browser sheet tokenizer (features/webui/static/highlight.js) mirrors
SubmarineOutput.sublime-syntax. Runs the JS under node; skipped without it."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HL = os.path.join(ROOT, "features", "webui", "static", "highlight.js")
NODE = shutil.which("node")

RUNNER = r"""
require(process.argv[1]);
const lines = JSON.parse(process.argv[2]);
const st = SubmarineHL.initialState();
const out = [];
for (const line of lines) {
  const segs = SubmarineHL.tokenizeLine(line, st).filter((s) => s[1]);
  out.push({ segs: segs, ctx: st.ctx });
}
out.push({ html: SubmarineHL.toHtml(lines.join("\n")) });
process.stdout.write(JSON.stringify(out));
"""


def tokenize(lines):
    proc = subprocess.run([NODE, "-e", RUNNER, HL, json.dumps(lines)],
                          capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


@unittest.skipIf(NODE is None, "node is not installed")
class TestSheetTokenizer(unittest.TestCase):
    def classes(self, entry):
        return [c for c, _t in entry["segs"]]

    def test_turn_grammar(self):
        rows = tokenize([
            "◎ fix the bug ▶",
            "",
            "⚙ Bash ×3",
            "✔ Read a.py → 12 matches",
            "✘ Bash failed",
            "│ output line",
            "☑ picked option 2",
            "  @done(opus)",
            "◎ ",
        ])
        self.assertEqual(self.classes(rows[0]), ["sm-prompt-marker", "sm-prompt", "sm-prompt-marker"])
        self.assertEqual(rows[0]["ctx"], "conversation")
        self.assertEqual(self.classes(rows[2]), ["sm-tool-background"])
        self.assertEqual(self.classes(rows[3]), ["sm-tool-done"])
        self.assertEqual(self.classes(rows[4]), ["sm-tool-error"])
        self.assertEqual(self.classes(rows[5]), ["sm-tool-output"])
        self.assertEqual(self.classes(rows[6]), ["sm-question-answered"])
        self.assertEqual(self.classes(rows[7]), ["sm-meta-done"])
        self.assertEqual(rows[7]["ctx"], "main")           # @done pops the turn
        self.assertEqual(self.classes(rows[8]), ["sm-prompt-marker"])

    def test_markdown_and_fences(self):
        rows = tokenize([
            "◎ q ▶",
            "## Heading",
            "Here **bold** and `code` and [t](http://x)",
            "- item",
            "```python",
            "def f(x):  # c",
            "    return 'a' + 42",
            "```",
            "```diff",
            "+added",
            "-gone",
            " ctx",
            "```",
            "after",
        ])
        self.assertIn("sm-markup-heading", self.classes(rows[1]))
        c2 = self.classes(rows[2])
        for want in ("sm-markup-bold", "sm-markup-raw-inline", "sm-markup-underline-link"):
            self.assertIn(want, c2)
        self.assertIn("sm-markup-list", self.classes(rows[3]))
        self.assertEqual(rows[4]["ctx"], "code")
        self.assertIn("sm-keyword", self.classes(rows[5]))
        self.assertIn("sm-entity-name-function", self.classes(rows[5]))
        self.assertIn("sm-comment", self.classes(rows[5]))
        self.assertIn("sm-string", self.classes(rows[6]))
        self.assertIn("sm-constant-numeric", self.classes(rows[6]))
        self.assertEqual(rows[7]["ctx"], "conversation")   # closing fence pops
        self.assertEqual(self.classes(rows[9]), ["sm-markup-inserted"])
        self.assertEqual(self.classes(rows[10]), ["sm-markup-deleted"])
        self.assertEqual(self.classes(rows[11]), ["sm-diff-context"])
        self.assertEqual(self.classes(rows[13]), ["sm-text"])

    def test_multiline_prompt_and_open_composer(self):
        rows = tokenize(["◎ first", "second ▶", "reply", "◎ ", "draft"])
        self.assertEqual(rows[0]["ctx"], "prompt")
        self.assertEqual(self.classes(rows[1]), ["sm-prompt", "sm-prompt-marker"])
        self.assertEqual(rows[1]["ctx"], "conversation")
        self.assertEqual(rows[3]["ctx"], "prompt")          # the sticky composer
        self.assertEqual(self.classes(rows[4]), ["sm-prompt"])

    def test_html_is_escaped_and_code_lines_marked(self):
        rows = tokenize(["◎ q ▶", "<b>not html</b>", "```", "x < y", "```"])
        html = rows[-1]["html"]
        self.assertNotIn("<b>", html)
        self.assertIn("&lt;b&gt;", html)
        self.assertIn('class="sm-code-line"', html)


if __name__ == "__main__":
    unittest.main()
