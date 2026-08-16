"""acp-term-* and bash-* for the same tool_use must notify once."""
from __future__ import annotations

import os
import unittest

from core.background import alias_bg_task_ids, bg_notify_already, mark_bg_notify_ids

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestBgNotifyDedupe(unittest.TestCase):
    def setUp(self):
        self.s = {
            "_task_tool_map": {
                "acp-term-abc": "tool-1",
                "bash-xyz": "tool-1",
                "bash-other": "tool-2",
            },
            "_bg_notified_task_ids": set(),
            "_bg_notified_tool_ids": set(),
            "_pending_bg_task_ids": set(),
            "_pending_bg_tool_ids": set(),
        }

    def test_core_has_alias_helpers(self):
        path = os.path.join(_ROOT, "core", "background.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("def alias_bg_task_ids", src)
        self.assertIn("def bg_notify_already", src)
        self.assertIn("def mark_bg_notify_ids", src)

    def test_aliases_include_both_ids(self):
        ids = alias_bg_task_ids(self.s["_task_tool_map"], "tool-1", "acp-term-abc")
        self.assertEqual(ids, {"acp-term-abc", "bash-xyz"})

    def test_second_id_for_same_tool_is_already(self):
        self.assertFalse(bg_notify_already(self.s, "acp-term-abc", "tool-1"))
        mark_bg_notify_ids(self.s, "acp-term-abc", "tool-1")
        self.assertTrue(bg_notify_already(self.s, "bash-xyz", "tool-1"))
        self.assertTrue(bg_notify_already(self.s, "bash-xyz", ""))
        self.assertFalse(bg_notify_already(self.s, "bash-other", "tool-2"))


if __name__ == "__main__":
    unittest.main()
