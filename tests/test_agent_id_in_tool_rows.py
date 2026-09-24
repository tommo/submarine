"""Agent ids in tool rows are whole and canonical, so Cmd+click finds them.

signal_complete shows the parent it reports to — the jump back to it.
"""
from __future__ import annotations

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.agent_ids import ID_IN_TEXT_RE
from core.registry import default_registry
from ui import formatters as F


def _tool(status, inp, result=None):
    return types.SimpleNamespace(status=status, tool_input=inp, result=result,
                                 name="mcp__submarine__signal_complete")


class AgentIdInToolRowsTest(unittest.TestCase):
    def setUp(self):
        default_registry.clear()
        self.out = types.SimpleNamespace(_format_mcp_result=lambda r: "")

    def tearDown(self):
        default_registry.clear()

    def test_signal_complete_names_the_parent_from_the_result(self):
        row = F._signal_complete(self.out, _tool(
            "done", {"result_summary": "Patched it"},
            {"status": "ok", "parent_agent_id": "agent-0123456789ab"}))
        self.assertIn("parent submarine::0123456789ab", row)
        self.assertEqual(ID_IN_TEXT_RE.findall(row), ["submarine::0123456789ab"])

    def test_signal_complete_names_the_parent_before_the_result(self):
        me = types.SimpleNamespace(output=self.out, agent_id="submarine::bbbbbbbbbbbb",
                                   parent_agent_id="submarine::aaaaaaaaaaaa")
        default_registry.by_agent[me.agent_id] = me
        row = F._signal_complete(self.out, _tool("running", {"result_summary": "x"}))
        self.assertIn("parent submarine::aaaaaaaaaaaa", row)
        self.assertLess(row.index("parent"), row.index("x"))

    def test_ids_are_never_clipped(self):
        row = F._read_session_edits(self.out, _tool(
            "running", {"agent_id": "submarine::0123456789ab"}))
        self.assertIn("submarine::0123456789ab", row)
        row = F._send_to_session(self.out, _tool(
            "running", {"agent_id": "agent-0123456789ab", "prompt": "hi"}))
        self.assertIn("submarine::0123456789ab", row)


class SendMessageRowTest(unittest.TestCase):
    def test_the_row_names_the_recipient_and_the_message(self):
        tool = types.SimpleNamespace(status="done", name="SendMessage", result=None,
                                     tool_input={"to": "explorer",
                                                 "message": "Also check\nthe tests"})
        row = F._send_message(None, tool)
        self.assertEqual(row, ": → explorer · Also check the tests")
        self.assertIs(F.TOOL_FORMATTERS["SendMessage"], F._send_message)


if __name__ == "__main__":
    unittest.main()
