"""The `@done(…)` line names the provider THIS turn ran on.

In single-view mode every session binds the same host view, so the provider
stamps on that view describe whichever session is bound right now. Reading
them at paint time put another session's provider on a finished turn
(`@done(487.2s, (CC) StepFun/gpt-5.6-sol…)` on a Claude sheet), and a repaint
of old turns dropped the provider entirely. Each turn now keeps its own.
"""
from __future__ import annotations

import unittest

from tests.test_composer_always_back import _live
from tests.test_single_view import RecordingWindow, _SingleViewCase
from ui import keys
from ui.host import HostView


def _tail(out, n=90):
    return out.view.substr(None)[-n:]


class TestDoneProvider(_SingleViewCase):
    def setUp(self):
        super().setUp()
        self.win = RecordingWindow()
        self.hv = HostView.for_window(self.win)

    def _session(self, name, label, model):
        s, out, client = _live(self.win, name)
        s.provider_label = label
        s.model = model
        s.effort = "high"
        return s, out, client

    def test_a_stale_stamp_cannot_rename_the_provider(self):
        s, out, _c = self._session("a", "Claude", "opus")
        self.hv.attach(self.win, s)
        # Left by the session that had the host view before this one.
        keys.write_setting(out.view.settings(), keys.PROVIDER_LABEL, "(CC) StepFun")
        keys.write_setting(out.view.settings(), keys.MODEL, "gpt-5.6-sol")
        s.query("one")
        s.events.text({"text": "done"})
        out.meta(487.2)
        self.assertIn("@done(487.2s, opus, effort:high)", _tail(out))
        self.assertNotIn("StepFun", out.view.substr(None))

    def test_each_session_keeps_its_own_after_a_swap(self):
        a, ao, _ac = self._session("a", "Claude", "opus")
        b, bo, _bc = self._session("b", "(CC) StepFun", "gpt-5.6-sol")
        self.hv.attach(self.win, a)
        a.query("one"); a.events.text({"text": "x"}); ao.meta(12.0)
        # b finishes while a is on screen (viewless), then we switch.
        b.query("two"); b.events.text({"text": "y"}); bo.meta(487.2)
        self.hv.attach(self.win, b)
        self.assertIn("@done(487.2s, (CC) StepFun/gpt-5.6-sol, effort:high)", _tail(bo, 120))
        self.hv.attach(self.win, a)
        self.assertIn("@done(12.0s, opus, effort:high)", _tail(ao))

    def test_a_turn_with_no_identity_says_nothing_about_a_provider(self):
        """An old buffer / replayed transcript: a duration, not a borrowed
        provider."""
        from ui.models import Conversation
        s, out, _c = self._session("a", "Claude", "opus")
        self.hv.attach(self.win, s)
        keys.write_setting(out.view.settings(), keys.PROVIDER_LABEL, "(CC) StepFun")
        keys.write_setting(out.view.settings(), keys.MODEL, "gpt-5.6-sol")
        old = Conversation(prompt="old", working=False, duration=3.0, has_meta=True)
        self.assertEqual(out.renderer._meta_line(old), "\n  @done(3.0s)\n")


if __name__ == "__main__":
    unittest.main()
