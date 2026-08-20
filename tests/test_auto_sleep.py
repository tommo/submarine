"""Auto-sleep must not treat a just-interrupted long turn as hours idle."""
from __future__ import annotations

import unittest

from core.session import auto_sleep_due
from core.turn import TurnController


class _Sess:
    def __init__(self, **kw):
        self.sleep_disabled = False
        self.quick_mode = False
        self._interrupting = False
        self.goal_tracker = None
        self.initialized = True
        self.working = False
        self.is_sleeping = False
        self.last_idle_at = 0.0
        self.last_activity = 0.0
        self.name = "kimi"
        self.turn = None
        self.__dict__.update(kw)


class TestAutoSleepDue(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000.0
        self.timeout = 60

    def test_long_turn_then_interrupt_stamp_is_not_due(self):
        # Turn started 90m ago; Esc just stamped last_activity/last_idle_at.
        s = _Sess(last_idle_at=self.now, last_activity=self.now)
        due, _ = auto_sleep_due(s, self.now, self.timeout)
        self.assertFalse(due)

    def test_stale_clock_after_long_turn_would_have_slept(self):
        started = self.now - (90 * 60)
        s = _Sess(last_idle_at=started, last_activity=started)
        due, idle_at = auto_sleep_due(s, self.now, self.timeout)
        self.assertTrue(due)
        self.assertEqual(idle_at, started)

    def test_inbound_activity_during_idle_looking_kimi_turn(self):
        started = self.now - (90 * 60)
        s = _Sess(last_idle_at=started, last_activity=self.now, working=False)
        due, _ = auto_sleep_due(s, self.now, self.timeout)
        self.assertFalse(due)

    def test_skip_while_interrupting(self):
        started = self.now - (90 * 60)
        s = _Sess(
            last_idle_at=started, last_activity=started, _interrupting=True)
        due, _ = auto_sleep_due(s, self.now, self.timeout)
        self.assertFalse(due)

    def test_skip_while_turn_kind_interrupting(self):
        started = self.now - (90 * 60)
        turn = TurnController()
        turn.begin_query()
        turn.begin_interrupt()
        s = _Sess(
            last_idle_at=started, last_activity=started,
            working=False, turn=turn)
        due, _ = auto_sleep_due(s, self.now, self.timeout)
        self.assertFalse(due)

    def test_skip_while_working(self):
        started = self.now - (90 * 60)
        s = _Sess(last_idle_at=started, last_activity=started, working=True)
        due, _ = auto_sleep_due(s, self.now, self.timeout)
        self.assertFalse(due)


if __name__ == "__main__":
    unittest.main()
