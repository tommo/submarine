"""Undo skips synthetic channel/timer/inject turns."""
from __future__ import annotations

import os
import tempfile
import unittest

from core.rewind import (
    find_rewind_point,
    find_session_jsonl,
    is_synthetic_turn,
    last_prompt_span,
    prompt_index_span,
    prompt_spans,
    read_claude_turns,
    turns_for_undo,
    uses_claude_jsonl,
)


class TestClaudeJsonlRouting(unittest.TestCase):
    def test_cc_providers_use_claude_jsonl(self):
        self.assertTrue(uses_claude_jsonl("claude"))
        self.assertTrue(uses_claude_jsonl("deepseek"))
        self.assertTrue(uses_claude_jsonl("stepfun"))
        self.assertFalse(uses_claude_jsonl("grok"))
        self.assertFalse(uses_claude_jsonl("codex"))
        self.assertFalse(uses_claude_jsonl("kimi"))


class TestLastPromptSpan(unittest.TestCase):
    def test_cuts_from_the_last_submitted_prompt(self):
        text = "◎ first ▶\nhello\n◎ second ▶\nworld\n◎ \n"
        span = last_prompt_span(text)
        self.assertEqual(span, (text.index("\n◎ second"), len(text)))
        cut = text[: span[0]]
        self.assertIn("◎ first ▶", cut)
        self.assertNotIn("second", cut)

    def test_only_prompt_at_file_start(self):
        text = "◎ only ▶\nreply"
        self.assertEqual(last_prompt_span(text), (0, len(text)))

    def test_grok_prompt_index_strips_from_that_turn(self):
        text = "◎ first ▶\na\n◎ second ▶\nb\n◎ third ▶\nc"
        self.assertEqual(len(prompt_spans(text)), 3)
        start, end = prompt_index_span(text, 1)
        self.assertEqual(end, len(text))
        self.assertIn("first", text[:start])
        self.assertNotIn("second", text[:start])


class TestSyntheticTags(unittest.TestCase):
    def test_keeps_removed_feature_tags(self):
        self.assertTrue(is_synthetic_turn("<channel>ping</channel>"))
        self.assertTrue(is_synthetic_turn("<timer>60s</timer>"))
        self.assertTrue(is_synthetic_turn("<inject>wake</inject>"))
        self.assertTrue(is_synthetic_turn("<task-notification>done</task-notification>"))
        self.assertFalse(is_synthetic_turn("please review the diff"))


class TestClaudeTurns(unittest.TestCase):
    def test_skips_tool_result_and_meta(self):
        raw = "\n".join([
            '{"type":"assistant","uuid":"u1","message":{"content":[{"type":"text","text":"hi"}]}}',
            '{"type":"user","message":{"content":[{"type":"text","text":"first"}]}}',
            '{"type":"assistant","uuid":"u2","message":{"content":[{"type":"text","text":"ok"}]}}',
            '{"type":"user","isMeta":true,"message":{"content":"skip"}}',
            '{"type":"user","message":{"content":[{"type":"tool_result","content":"x"}]}}',
            '{"type":"user","message":{"content":[{"type":"text","text":"<inject>bg</inject>"}]}}',
            '{"type":"assistant","uuid":"u3"}',
            '{"type":"user","message":{"content":[{"type":"text","text":"real two"}]}}',
        ])
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.write(fd, raw.encode("utf-8"))
        os.close(fd)
        try:
            turns = read_claude_turns(path)
            self.assertEqual([p for p, _u in turns], ["first", "<inject>bg</inject>", "real two"])
            undo = turns_for_undo(turns)
            labels = [u[2] for u in undo]
            self.assertIn("real two", labels)
            self.assertIn("first", labels)
            self.assertNotIn("<inject>bg</inject>", labels)
            rid, undone = find_rewind_point(turns)
            self.assertEqual(undone, "real two")
            self.assertEqual(rid, "u3")
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
