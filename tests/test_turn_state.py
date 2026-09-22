"""TurnState closer table + host must not adopt leftover into working."""
from __future__ import annotations

import os
import unittest

from core.turn import TurnController

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestHostDoesNotAdoptWorking(unittest.TestCase):
    def _live(self, path, name):
        with open(path, encoding="utf-8") as f:
            src = f.read()
        start = src.find("def %s" % name)
        self.assertGreater(start, 0, name)
        nxt = src.find("\n    def ", start + 10)
        if nxt < 0:
            nxt = len(src)
        out = []
        for line in src[start:nxt].splitlines():
            s = line.lstrip()
            if s.startswith("#"):
                continue
            out.append(line)
        return "\n".join(out)

    def test_adopt_does_not_assign_working(self):
        body = self._live(
            os.path.join(_ROOT, "core", "session.py"), "_adopt_agent_turn")
        self.assertNotIn("self.working = True", body)
        self.assertIn("return", body)

    def test_completion_never_starts_a_turn(self):
        body = self._live(
            os.path.join(_ROOT, "core", "background.py"), "_complete")
        self.assertNotIn("self._adopt_agent_turn(", body)
        self.assertNotIn("_bg_soft_fallback_query", body)
        self.assertNotIn("on_query", body)
        self.assertNotIn("begin_query", body)

    def test_query_queues_while_busy(self):
        body = self._live(
            os.path.join(_ROOT, "core", "session.py"), "query")
        self.assertIn("should_queue_prompt", body)
        self.assertIn("queue_prompt", body)


class TestTurnControllerSingleBusy(unittest.TestCase):
    def test_working_is_busy(self):
        t = TurnController()
        self.assertFalse(t.working)
        t.begin_query()
        self.assertTrue(t.working)
        self.assertTrue(t.busy)
        t.end_live()
        self.assertFalse(t.working)

    def test_stale_gen_guard(self):
        t = TurnController()
        g1 = t.begin_query()
        g2 = t.begin_query()
        self.assertFalse(t.matches_gen(g1))
        self.assertTrue(t.matches_gen(g2))
        self.assertFalse(t.end_live(g1))
        self.assertEqual(t.kind, "live")


if __name__ == "__main__":
    unittest.main()
