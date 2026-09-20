"""Port of sublime-claude sticky-composer / input-area management.

Covers Session._enter_input_with_draft (mid-stream ◎, draft restore, gates)
and OutputSheet text-anchor viewport pin/restore.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import FakeClient, FakeOutput, make_session
from tests.test_headless_render import RecordingView, RecordingWindow, _Region
from ui.sheet import OutputSheet
from ui.view import SubmarineOutputView


class _SheetOwner(object):
    conversations = []
    current = None


class TestInterruptLeavesComposerClean(unittest.TestCase):
    def test_interrupted_banner_does_not_land_in_the_composer(self):
        """Sticky ◎ was open; interrupt used to paint ◎ nterrupted]*."""
        from tests.test_headless_render import RecordingView, RecordingWindow

        win = RecordingWindow()
        out = SubmarineOutputView(win)
        v = RecordingView(view_id=9)
        v._window = win
        out.view = v
        out.prompt("WTF ARE YOU TALKING ABOUT")
        out.text("nope")
        out.enter_input_mode()
        self.assertTrue(out.is_input_mode())
        self.assertIn("◎", v._content)
        out.interrupted()
        text = v._content
        self.assertIn("*[interrupted]*", text)
        self.assertNotIn("◎ nterrupted", text)
        # Banner, then a fresh ◎ on the following line.
        banner_at = text.rfind("*[interrupted]*")
        marker_at = text.rfind("◎")
        self.assertGreater(marker_at, banner_at)
        after_banner = text[banner_at:]
        self.assertRegex(after_banner, r"\*\[interrupted\]\*\s*\n◎")


class TestEnterInputWithDraft(unittest.TestCase):
    def _session(self, **kwargs):
        client = kwargs.pop("client", FakeClient())
        out = kwargs.pop("output", None) or FakeOutput()
        s = make_session(initialized=True, client=client, output=out, **kwargs)
        return s, out

    def test_idle_wrapper_skips_while_working(self):
        s, out = self._session()
        s.turn.begin_query()
        self.assertTrue(s.working)
        s._enter_input_if_idle()
        self.assertEqual(out.input_enters, 0)

    def test_with_draft_opens_while_working(self):
        s, out = self._session()
        s.turn.begin_query()
        s._enter_input_with_draft()
        self.assertEqual(out.input_enters, 1)
        self.assertTrue(out.is_input_mode())
        self.assertTrue(s._input_mode_entered)

    def test_restores_draft_text(self):
        s, out = self._session()
        s.draft_prompt = "keep typing"
        s._enter_input_with_draft()
        self.assertEqual(out.composer_text, "keep typing")
        self.assertEqual(out.input_enters, 1)

    def test_whitespace_draft_is_dropped(self):
        s, out = self._session()
        s.draft_prompt = "\n  \n"
        s._enter_input_with_draft()
        self.assertEqual(out.composer_text, "")
        self.assertEqual(s.draft_prompt, "")

    def test_skips_sleeping(self):
        s, out = self._session()
        s.session_id = "abc"
        s.client = None
        s.initialized = False
        self.assertTrue(s.is_sleeping)
        s._enter_input_with_draft()
        self.assertEqual(out.input_enters, 0)

    def test_skips_when_composer_not_allowed(self):
        s, out = self._session()
        s._composer_allowed = False
        s._enter_input_with_draft()
        self.assertEqual(out.input_enters, 0)

    def test_skips_modal_ui(self):
        s, out = self._session()
        out.questions.append(("q", [{"text": "x"}], None))
        s._enter_input_with_draft()
        self.assertEqual(out.input_enters, 0)

    def test_already_open_refreshes_hints(self):
        s, out = self._session()
        out.enter_input_mode()
        before = out.input_enters
        s._enter_input_with_draft()
        self.assertEqual(out.input_enters, before)
        self.assertEqual(out.hint_refreshes, 1)
        self.assertGreater(s.last_idle_at, 0)

    def test_idle_wrapper_delegates_when_idle(self):
        s, out = self._session()
        s._enter_input_if_idle()
        self.assertEqual(out.input_enters, 1)
        self.assertEqual(out.collapsed_tails, 1)


class TestQueryStickyComposer(unittest.TestCase):
    def test_query_reopens_composer_after_send(self):
        out = FakeOutput()
        s = make_session(initialized=True, client=FakeClient(), output=out)
        s.query("hello")
        self.assertTrue(s.working)
        self.assertEqual(out.input_enters, 0)
        s.scheduler.fire_due(40)
        self.assertEqual(out.input_enters, 1)
        self.assertTrue(out.is_input_mode())

    def test_silent_query_does_not_reopen(self):
        out = FakeOutput()
        s = make_session(initialized=True, client=FakeClient(), output=out)
        s.query("hello", silent=True)
        s.scheduler.fire_due(40)
        self.assertEqual(out.input_enters, 0)

    def test_user_submit_clears_draft(self):
        out = FakeOutput()
        s = make_session(initialized=True, client=FakeClient(), output=out)
        s.draft_prompt = "leftover"
        s.query("hello")
        self.assertEqual(s.draft_prompt, "")

    def test_open_composer_preserves_draft_on_query(self):
        out = FakeOutput()
        out.enter_input_mode()
        out.composer_text = "still typing"
        s = make_session(initialized=True, client=FakeClient(), output=out)
        s.query("hello")
        self.assertEqual(s.draft_prompt, "still typing")


class TestViewportPin(unittest.TestCase):
    def _sheet(self, content="hello\nworld\nmore\n"):
        view = RecordingView()
        view._content = content
        view._vp = (0.0, 8.0)
        view._sel.add(_Region(2, 2))
        sheet = OutputSheet(_SheetOwner(), RecordingWindow())
        sheet.view = view
        return sheet, view

    def test_pin_uses_text_anchor_not_raw_pixels(self):
        sheet, view = self._sheet()
        pin = sheet.pin_view_state()
        self.assertIn("anchor", pin)
        self.assertIn("y_off", pin)
        self.assertEqual(pin["anchor"], 8)  # layout_to_text((0, 8))
        self.assertEqual(pin["y_off"], 0.0)
        self.assertEqual(pin["sels"], [(2, 2)])

    def test_restore_composer_caret_wins(self):
        sheet, view = self._sheet("abcdef")
        pin = sheet.pin_view_state()
        pin["composer_caret"] = 4
        pin["sels"] = [(0, 0)]
        sheet.restore_view_state(pin)
        self.assertEqual(len(view.sel()), 1)
        sel = view.sel()[0]
        begin = sel.begin() if hasattr(sel, "begin") else sel.a
        self.assertEqual(begin, 4)

    def test_schedule_keeps_the_latest_pin(self):
        sheet, _view = self._sheet()
        sheet.schedule_viewport_restore({"anchor": 1})
        sheet.schedule_viewport_restore({"anchor": 2})
        self.assertEqual(sheet._pending_vp_pin["anchor"], 2)


class TestSubmitKeepsUserLine(unittest.TestCase):
    def _bound(self, transcript="old turn\n"):
        win = RecordingWindow()
        out = SubmarineOutputView(win)
        v = RecordingView()
        v._window = win
        v._content = transcript
        v._vp = (0.0, 4.0)
        out.view = v
        return out, v

    def test_promote_does_not_move_the_user_line(self):
        out, v = self._bound("old turn\n")
        out.enter_input_mode()
        self.assertTrue(out.is_input_mode())
        out.set_composer_text("hello")
        marker = out.composer._input_start - len(out.composer._input_marker)
        self.assertEqual(v._content[marker:marker + 1], "◎")

        out.prompt("hello")

        self.assertFalse(out.is_input_mode())
        idx = v._content.find("◎ hello ▶")
        self.assertGreaterEqual(idx, 0)
        # Same ◎ offset — not wiped and rewritten further down the buffer.
        self.assertEqual(idx, marker)
        self.assertEqual(out.current.prompt, "hello")
        self.assertIsNotNone(out.current.region)

    def test_sticky_reopen_keeps_the_prompt_and_adds_a_new_composer(self):
        out, v = self._bound("old turn\n")
        out.enter_input_mode()
        out.set_composer_text("hello")
        marker = out.composer._input_start - len(out.composer._input_marker)
        out.prompt("hello")
        out.enter_input_mode()
        self.assertTrue(out.is_input_mode())
        self.assertEqual(v._content.find("◎ hello ▶"), marker)
        # New empty ◎ is after the submitted line, not a rewrite of it.
        self.assertGreater(out.composer._input_start, marker)

    def test_query_consumes_matching_draft(self):
        out = FakeOutput()
        out.enter_input_mode()
        out.composer_text = "hello"
        s = make_session(initialized=True, client=FakeClient(), output=out)
        s.query("hello", display_prompt="hello")
        self.assertEqual(s.draft_prompt, "")

    def test_prompt_does_not_stash_submitted_text_as_draft(self):
        out, v = self._bound("old turn\n")
        sess = type("S", (), {"draft_prompt": "keep?", "pending_context": None})()
        out.enter_input_mode()
        out.set_composer_text("hello")
        import ui.renderer as R
        import ui.composer as C
        orig_r, orig_c = R.get_session_for_view, C.get_session_for_view
        R.get_session_for_view = lambda view, s=sess: s
        C.get_session_for_view = lambda view, s=sess: s
        try:
            out.prompt("hello")
        finally:
            R.get_session_for_view = orig_r
            C.get_session_for_view = orig_c
        self.assertEqual(sess.draft_prompt, "")

    def test_enter_draft_does_not_restore_the_current_prompt(self):
        out = FakeOutput()
        out.current = type("T", (), {"prompt": "hello"})()
        s = make_session(initialized=True, client=FakeClient(), output=out)
        s.draft_prompt = "hello"
        s._enter_input_with_draft()
        self.assertEqual(s.draft_prompt, "")
        self.assertEqual(out.composer_text, "")


if __name__ == "__main__":
    unittest.main()
