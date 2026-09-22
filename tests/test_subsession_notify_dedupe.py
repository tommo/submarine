"""One parent notification per child completion — not stock + custom."""
from __future__ import annotations

import unittest

from core.registry import (
    SessionRegistry,
    child_parent_already_notified,
    is_stock_subsession_wake,
    mark_child_parent_notified,
    merge_subsession_queue,
    parent_notify_should_inject,
    subsession_notify_key,
)

STOCK = (
    "✅ Subsession agent-ae635c6b3cb2 completed "
    "(agent_id=agent-ae635c6b3cb2, view_id=294)"
)
CUSTOM = (
    "Codex design consultant (skin-editor-design / "
    "agent-ae635c6b3cb2) finished. Memo is on disk."
)


class TestMergeSubsessionQueue(unittest.TestCase):
    def test_same_key(self):
        self.assertEqual(
            subsession_notify_key(STOCK),
            subsession_notify_key(CUSTOM),
        )
        self.assertTrue(is_stock_subsession_wake(STOCK))
        self.assertFalse(is_stock_subsession_wake(CUSTOM))

    def test_stock_then_custom_keeps_custom(self):
        q = merge_subsession_queue([STOCK], CUSTOM)
        self.assertEqual(q, [CUSTOM])

    def test_custom_then_stock_keeps_custom(self):
        q = merge_subsession_queue([CUSTOM], STOCK)
        self.assertEqual(q, [CUSTOM])

    def test_other_prompts_untouched(self):
        q = merge_subsession_queue(["hello"], STOCK)
        self.assertEqual(q, ["hello", STOCK])

    def test_unrelated_child_not_merged(self):
        other = "✅ Subsession agent-ffffffffffff completed"
        q = merge_subsession_queue([STOCK], other)
        self.assertEqual(q, [STOCK, other])


class _Child:
    def __init__(self):
        self.agent_id = "agent-ae635c6b3cb2"
        self.subsession_id = "agent-ae635c6b3cb2"
        self._parent_notified = False


class TestAlreadyNotified(unittest.TestCase):
    def test_inject_xor_waiter_same_completion_only(self):
        """Waiter delivered this complete → do not also inject/queue."""
        self.assertTrue(parent_notify_should_inject(0))
        self.assertFalse(parent_notify_should_inject(1))

    def test_next_complete_can_inject_again(self):
        c = _Child()
        mark_child_parent_notified(c)
        self.assertTrue(child_parent_already_notified(c))
        # Lifetime flag must not block a later round.
        self.assertTrue(parent_notify_should_inject(0, c))
        reg = SessionRegistry()
        self.assertEqual(reg.fire_subsession_waits(c, "done"), 0)


if __name__ == "__main__":
    unittest.main()


class TestSubsessionCompletionRow(unittest.TestCase):
    """The parent's sheet shows a child's completion as one short row — the
    child's name — never the report the wake prompt carries."""

    def _pair(self, parent_working):
        from core.registry import SessionRegistry
        from tests.fakes import FakeClient, make_session
        reg = SessionRegistry()
        parent = make_session(client=FakeClient(), initialized=True, registry=reg)
        parent.agent_id = "agent-000000000001"
        child = make_session(client=FakeClient(), initialized=True, registry=reg)
        child.agent_id = "agent-000000000002"
        child.name = "opus-assistant"
        child.parent_agent_id = parent.agent_id
        reg.register_session(parent)
        reg.register_session(child)
        if parent_working:
            parent.query("long job")
        return reg, parent, child

    def test_idle_parent_gets_a_named_row(self):
        reg, parent, child = self._pair(parent_working=False)
        reg.register_subsession_wait(child.agent_id, parent_agent_id=parent.agent_id,
                                     wake_prompt="Review the report, then verify.")
        reg.fire_subsession_waits(child, result_summary="## Report\n" + "x" * 500)
        self.assertEqual(parent.output.prompts[-1][0], "📬 opus-assistant finished")
        sent = [(p or {}).get("prompt") for m, p, _cb in parent.client.sent if m == "query"]
        self.assertIn("## Report", sent[-1], "the model still gets the report")

    def test_busy_parent_queues_it_under_the_same_row(self):
        reg, parent, child = self._pair(parent_working=True)
        parent.client.sent.clear()
        reg.register_subsession_wait(child.agent_id, parent_agent_id=parent.agent_id,
                                     wake_prompt="Review the report, then verify.")
        reg.fire_subsession_waits(child, result_summary="## Report\n" + "x" * 500)
        self.assertEqual(parent.chrome.queues[-1], ["📬 opus-assistant finished"])
        # The inject was sent mid-turn; simulate the bridge saying idle so it
        # fires as its own turn when this one closes.
        inj = [c for c in parent.client.sent if c[0] == "inject_message"]
        self.assertEqual(len(inj), 1)
        inj[0][2]({"result": {"status": "idle"}})
        self.assertIn("## Report", parent._queued_prompts[0])
        parent._on_done({"status": "complete"}, _expected_gen=parent.turn.gen)
        self.assertEqual(parent.output.prompts[-1][0], "📬 opus-assistant finished")
