"""Undo skips synthetic channel/timer/inject turns."""
from __future__ import annotations

import os
import tempfile
import unittest

from core.rewind import (
    find_rewind_point,
    is_synthetic_turn,
    read_claude_turns,
    turns_for_undo,
)


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
