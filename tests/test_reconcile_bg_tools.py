"""Dead ⚙ must clear when bridge running=[] even if never seen live."""
import os
import unittest

from core.background import BackgroundTaskGate
from core.turn import TurnController


class _Tool:
    def __init__(self):
        self.status = "background"
        self.name = "Bash"


class _Out:
    def find_tool_by_id(self, tid):
        return None

    def tool_done(self, *a, **k):
        pass

    def remove_tool(self, tool):
        pass

    def is_input_mode(self):
        return False

    def refresh_background_hints(self):
        pass


class _Sched:
    def call_later(self, ms, fn):
        return None


class TestReconcileUnseenDeadGear(unittest.TestCase):
    def _gate(self, backend="kimi"):
        turn = TurnController()
        g = BackgroundTaskGate(turn, _Sched(), _Out(), backend=backend)
        g.bg_tools = {"8:tool_GwLP": _Tool()}
        g.bg_task_ids = {"8:tool_GwLP"}
        g.task_tool_map = {"bash-0eaakzzi": "8:tool_GwLP"}
        g.finalized = []
        g.woke = []

        def _fin(tool_use_id, keep, result=None, error=False):
            g.finalized.append((tool_use_id, keep))
            t = g.bg_tools.get(tool_use_id)
            if t is not None:
                t.status = "done"
            g.bg_tools.pop(tool_use_id, None)

        def _notify(data, working=False):
            g.woke.append(data)
            g.task_tool_map.pop(data.get("task_id"), None)

        g.finalize_tool = _fin
        g.on_task_notification = _notify
        return g

    def test_kimi_unseen_completed_finalizes_without_wake(self):
        g = self._gate("kimi")
        g.reconcile(running=[])
        self.assertIn(("8:tool_GwLP", True), g.finalized)
        self.assertEqual(g.woke, [])
        self.assertNotIn("8:tool_GwLP", g.bg_task_ids)
        self.assertNotIn("bash-0eaakzzi", g.task_tool_map)

    def test_still_running_keeps_gear(self):
        g = self._gate("kimi")
        g.reconcile(running=["bash-0eaakzzi"])
        self.assertEqual(g.finalized, [])
        self.assertEqual(g.woke, [])
        self.assertIn("8:tool_GwLP", g.bg_task_ids)

    def test_grok_idle_vanished_finalizes_without_wake(self):
        """Grok self-wakes on the exit itself; a synthesized notification
        here was a second turn about the same job."""
        g = self._gate("grok")
        g.seen_running.add("bash-0eaakzzi")
        g.reconcile(running=[])
        self.assertIn(("8:tool_GwLP", True), g.finalized)
        self.assertEqual(g.woke, [])
        self.assertNotIn("bash-0eaakzzi", g.task_tool_map)

    def test_on_done_polls_bg(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "core", "session.py")
        with open(path, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("self.bg._poll()", body)
        self.assertIn("skip-dropped left", body)

    def test_task_updated_uses_payload_tool_id(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "core", "background.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        start = src.find("    def on_task_updated")
        nxt = src.find("\n    def ", start + 10)
        self.assertIn('data.get("tool_use_id")', src[start:nxt])


if __name__ == "__main__":
    unittest.main()
