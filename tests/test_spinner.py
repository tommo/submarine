"""The busy mark in the session sheet has to move.

Regression: the port kept the drawing (`ui/renderer.py` renders
`frames[_spinner_frame]`), the frames, the turn phases and the facade
(`output.advance_spinner`) — but nothing ever *called* it, so the mark sat on
frame 0 for the whole turn. The driver is `Session._animate`, ported from
sublime-claude, kicked on a phase change.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.session import (
    SPINNER_RESPOND_MS,
    SPINNER_TOOL_MS,
    SPINNER_WAIT_MS,
)
from plat.constants import SPINNER_RESPONDING, SPINNER_WAITING
from tests.fakes import FakeClient, FakeOutput, FakeScheduler, make_session


class SpinnerTest(unittest.TestCase):
    def _working(self, phase="waiting"):
        out = FakeOutput()
        client = FakeClient()
        s = make_session(output=out, client=client, initialized=True)
        s.turn.begin_query()
        s.turn_phase = phase
        return s, out, client

    def _frames_seen(self, out):
        return [f for f, _n in out.spinner_frames]

    def _step(self, session, times=1):
        """Advance the chain one frame at a time (`fire_due` cannot drain a
        callback that re-arms)."""
        for _ in range(times):
            session.scheduler.fire_next()

    def test_a_turn_starts_the_chain_and_the_mark_moves(self):
        s, out, _client = self._working("waiting")
        s._kick_animation()
        self.assertEqual(len(out.spinner_frames), 1, "one frame immediately")
        self.assertEqual(self._frames_seen(out), [SPINNER_WAITING])
        self._step(s, 3)
        self.assertEqual(len(out.spinner_frames), 4)
        self.assertTrue(all(f == SPINNER_WAITING for f in self._frames_seen(out)))
        self.assertEqual(s.scheduler.pending[-1][0], SPINNER_WAIT_MS)

    def test_the_phase_picks_the_frames_and_the_speed(self):
        s, out, _client = self._working("responding")
        s._kick_animation()
        self.assertEqual(self._frames_seen(out), [SPINNER_RESPONDING])
        self.assertEqual(s.scheduler.pending[-1][0], SPINNER_RESPOND_MS)
        s.turn_phase = "tool"
        self._step(s)
        self.assertEqual(self._frames_seen(out)[-1], SPINNER_RESPONDING)
        self.assertEqual(s.scheduler.pending[-1][0], SPINNER_TOOL_MS)

    def test_a_phase_change_kicks_the_chain(self):
        """The event port reports a phase per stream chunk, so only a *change*
        may kick — otherwise the mark would tick per event, not per interval."""
        s, out, _client = self._working("waiting")
        s._set_turn_phase("responding")
        self.assertEqual(len(out.spinner_frames), 1)
        # Same phase again (per-chunk "responding"): no extra chain.
        s._set_turn_phase("responding")
        s._set_turn_phase("responding")
        self.assertEqual(len(out.spinner_frames), 1)

    def test_the_chain_stops_when_the_turn_ends(self):
        s, out, _client = self._working("waiting")
        s._kick_animation()
        before = len(out.spinner_frames)
        s.turn.end_live()
        self._step(s, 3)
        self.assertEqual(len(out.spinner_frames), before, "no frame after the turn")
        self.assertEqual(s.turn_phase, "idle", "the end of a turn is idle")
        # Nothing is left armed once the turn is over.
        self.assertEqual(s.scheduler.pending, [])

    def test_the_chain_stops_when_the_bridge_is_gone(self):
        """`stop()` leaves the turn object alone but drops the client; the
        chain must not run forever against a dead session."""
        s, out, _client = self._working("waiting")
        s._kick_animation()
        s.client = None
        before = len(out.spinner_frames)
        self._step(s, 3)
        self.assertEqual(len(out.spinner_frames), before)
        self.assertEqual(s.scheduler.pending, [])

    def test_a_kick_never_leaves_two_chains_racing(self):
        s, out, _client = self._working("waiting")
        s._kick_animation()
        s._kick_animation()          # e.g. waiting → responding quickly
        self.assertEqual(len(out.spinner_frames), 2, "both kicked once")
        # The orphaned generation is still queued (the first kick armed it
        # before the second): it ticks once, finds a newer generation and dies
        # instead of re-arming, leaving exactly one live chain.
        for _ in range(4):
            if not s.scheduler.pending:
                break
            _ms, fn, _tok = s.scheduler.pending.pop(0)
            fn()
        self.assertEqual(len(s.scheduler.pending), 1, "one live chain, one tick armed")
        self._step(s)
        self.assertEqual(len(s.scheduler.pending), 1, "and it stays at one")

    def test_not_working_means_no_chain(self):
        out = FakeOutput()
        s = make_session(output=out, client=FakeClient(), initialized=True)
        s._kick_animation()
        self.assertEqual(out.spinner_frames, [])
        self.assertEqual(s.scheduler.pending, [])

    def test_spinner_does_not_full_render(self):
        """Busy-mark ticks must not `_render_current`. Patch the glyph only."""
        from ui.renderer import TurnRenderer
        from ui.models import Conversation
        from tests.test_single_view import RecordingView, _Region

        rendered = []
        titles = []
        patches = []

        class _Sheet(object):
            def is_following_tail(self, slack=120):
                return True

            def update_title(self):
                titles.append(1)

        class _Owner(object):
            def __init__(self):
                self.view = RecordingView()
                self.view._content = "◎ hi ▶\n  ◇\n"
                from ui import keys
                self.view._regions = {keys.CONV_REGION: [
                    _Region(0, len(self.view._content))]}
                self.sheet = _Sheet()
                self._modal = False
                self.composer = None
                self.modals = None

            def has_turn_modal_ui(self):
                return self._modal

            def _replace(self, start, end, text):
                patches.append((start, end, text))
                v = self.view
                v._content = v._content[:start] + text + v._content[end:]
                return start + len(text)

        owner = _Owner()
        r = TurnRenderer(owner)
        r._render_current = lambda auto_scroll=True: rendered.append(auto_scroll)
        r.current = Conversation(prompt="q", working=True)
        r._spinner_frames = SPINNER_WAITING
        r._spinner_frame = 0
        r.advance_spinner()
        self.assertEqual(rendered, [])
        self.assertTrue(patches)
        self.assertIn(patches[-1][2].strip(), list(SPINNER_WAITING))
        owner._modal = True
        n = len(patches)
        r.advance_spinner()
        self.assertEqual(rendered, [])
        self.assertEqual(len(patches), n)

    def test_a_viewless_sheet_still_counts_as_chrome_only(self):
        """The renderer guards on the view; the driver must not care, so a
        background turn keeps ticking (and paints nothing)."""
        s, out, _client = self._working("responding")
        out.view = None
        s._kick_animation()
        self.assertEqual(len(out.spinner_frames), 1)


if __name__ == "__main__":
    unittest.main()


class SpinnerInsertGuardTest(unittest.TestCase):
    """The insert path (no glyph in the span) runs once per conversation and
    shifts the composer before the edit, so a lost glyph cannot become a
    column of glyphs or a draft (the after-Cmd+K sheet full of ◆)."""

    def _owner(self):
        from tests.test_single_view import RecordingView, _Region
        from ui import keys
        events = []

        class _Composer(object):
            _question_input_mode = False

            def __init__(self):
                self._input_start = 8

            def is_input_mode(self):
                return True

            def peel_start(self):
                return 8

            def shift_anchors(self, d):
                events.append(("shift", d))
                self._input_start += d

        class _Sheet(object):
            def __init__(self, view):
                self.view = view

            def set_hidden_region(self, key, a, b):
                self.view._regions[key] = [_Region(a, b)]

            def is_following_tail(self, slack=120):
                return True

        class _Owner(object):
            def __init__(self):
                self.view = RecordingView()
                self.view._content = "◎ hi ▶\n◎ draft"
                self.view._regions = {keys.CONV_REGION: [_Region(0, 8)]}
                self.sheet = _Sheet(self.view)
                self.composer = _Composer()
                self.modals = None

            def has_turn_modal_ui(self):
                return False

            def _replace(self, start, end, text):
                events.append(("replace", start, text))
                v = self.view
                v._content = v._content[:start] + text + v._content[end:]
                return start + len(text)

        return _Owner(), events

    def test_one_insert_per_conversation_and_shift_before_edit(self):
        from ui.models import Conversation
        from ui.renderer import TurnRenderer

        owner, events = self._owner()
        r = TurnRenderer(owner)
        r.current = Conversation(prompt="q", working=True)
        r.current.region = (0, 8)
        self.assertTrue(r._patch_spinner_glyph("◇"))
        self.assertEqual([e[0] for e in events], ["shift", "replace"])
        # Pretend the glyph vanished from the span (a clear); no second insert.
        owner.view._content = "◎ hi ▶\n◎ draft"
        r.current.region = (0, 8)
        owner.composer._input_start = 8
        self.assertFalse(r._patch_spinner_glyph("◆"))
        self.assertEqual(owner.view._content.count("◆"), 0)
        # A new conversation may insert again.
        r.current = Conversation(prompt="q2", working=True)
        r.current.region = (0, 8)
        self.assertTrue(r._patch_spinner_glyph("◆"))

