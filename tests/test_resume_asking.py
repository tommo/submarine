"""Resume after interrupt must not restore asking UI."""
from __future__ import annotations

import os
import types
import unittest

from tests.fakes import FakeClient, make_session
from ui.modals import ModalUI

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Out:
    def __init__(self):
        self.pending_question = object()
        self.pending_permission = object()
        self.pending_plan = object()
        self._permission_queue = ["x"]
        self.composer = types.SimpleNamespace(_question_input_mode=True)
        self.view = None
        self._surface = {"modals": [{"kind": "question"}]}
        self.q = False
        self.p = False
        self.pl = False

    def clear_question(self):
        self.q = True

    def remove_permission_block(self):
        self.p = True

    def clear_plan_approval(self):
        self.pl = True


class TestResumeDropAsking(unittest.TestCase):
    def test_drop_cancels_and_interrupts_once(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, resume_id="sid-1")
        self.assertTrue(s._resume_drop_asking)
        sent = []
        s.client = types.SimpleNamespace(
            send=lambda m, p=None, acc=sent: acc.append((m, p)) or True)
        self.assertTrue(s._drop_resume_asking_if_needed(lambda: s.client.send(
            "question_response", {"id": 3, "answers": None})))
        self.assertTrue(s._drop_resume_asking_if_needed(lambda: s.client.send(
            "question_response", {"id": 4, "answers": None})))
        methods = [m for m, _ in sent]
        self.assertEqual(methods.count("interrupt"), 1)
        self.assertEqual(methods.count("question_response"), 2)
        self.assertEqual(s.output._asking_cleared, 2)

    def test_no_drop_when_flag_off(self):
        s = make_session(initialized=True, client=FakeClient())
        self.assertFalse(s._resume_drop_asking)
        called = []
        self.assertFalse(s._drop_resume_asking_if_needed(lambda: called.append(1)))
        self.assertEqual(called, [])

    def test_clear_asking_state_wipes_modals(self):
        ov = _Out()
        ui = ModalUI(ov)
        ui.pending_question = ov.pending_question
        ui.pending_permission = ov.pending_permission
        ui.pending_plan = ov.pending_plan
        ui._permission_queue = list(ov._permission_queue)
        ui.clear_question = ov.clear_question
        ui.remove_permission_block = ov.remove_permission_block
        ui.clear_plan_approval = ov.clear_plan_approval
        ui.clear_asking_state()
        self.assertTrue(ov.q)
        self.assertTrue(ov.p)
        self.assertTrue(ov.pl)
        self.assertIsNone(ui.pending_question)
        self.assertIsNone(ui.pending_permission)
        self.assertIsNone(ui.pending_plan)
        self.assertFalse(ov.composer._question_input_mode)
        self.assertEqual(ui._permission_queue, [])
        self.assertEqual(ov._surface.get("modals"), [])

    def test_query_clears_resume_drop_flag(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, resume_id="sid-1")
        self.assertTrue(s._resume_drop_asking)
        s.query("hello")
        self.assertFalse(s._resume_drop_asking)

    def test_permission_request_dropped_on_resume(self):
        client = FakeClient()
        s = make_session(initialized=True, client=client, resume_id="sid-1")
        s.events.permission_request({
            "id": 9, "tool": "Bash", "input": {"command": "ls"},
        })
        methods = [m for m, _p, _c in client.sent]
        self.assertIn("permission_response", methods)
        self.assertIn("interrupt", methods)
        self.assertEqual(s.output.permissions, [])

    def test_interrupted_does_not_dismiss_question_rpc(self):
        # callback(None) is "user dismissed" — Kimi continues. Interrupt must
        # session/cancel while elicitation is still outstanding.
        path = os.path.join(_ROOT, "ui", "renderer.py")
        with open(path) as f:
            src = f.read()
        start = src.find("    def interrupted")
        end = src.find("\n    def apply_plan_todos", start)
        body = src[start:end]
        self.assertNotIn("if callback:", body)
        self.assertIn("session/cancel", body)

    def test_cancel_unblocks_waiters_after_session_cancel(self):
        path = os.path.join(_ROOT, "bridge", "acp", "query.py")
        with open(path) as f:
            src = f.read()
        start = src.find("    async def _cancel_agent_turn")
        end = src.find("    async def handle_query", start)
        body = src[start:end]
        cancel_at = body.find('session/cancel"')
        unblock_at = body.find("_unblock_interaction_waiters")
        self.assertGreater(cancel_at, 0)
        self.assertGreater(unblock_at, cancel_at)


if __name__ == "__main__":
    unittest.main()
