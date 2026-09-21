"""The ◎ composer always comes back on a live, awake sheet.

Every path that peels it — an interrupt, a question (answered here or from
outside), an outside prompt, a spell viewless — has to end with it planted
again. These pin the paths that lost it.
"""
from __future__ import annotations

import unittest

from core.registry import default_registry
from tests.fakes import FakeClient, make_session
from tests.test_single_view import RecordingWindow, _SingleViewCase
from ui.host import HostView
from ui.view import SubmarineOutputView

Q = [{"header": "H", "question": "pick", "options": [{"label": "x"}, {"label": "y"}]}]


def _live(win, name):
    out = SubmarineOutputView(win)
    client = FakeClient()
    s = make_session(output=out, chrome=out, window=win, registry=default_registry,
                     client=client, initialized=True)
    s.name = name
    s.session_id = "sess-" + name
    s._composer_allowed = True
    return s, out, client


def _answer_bridge(s, client, result):
    _m, _p, cb = [c for c in client.sent if c[0] == "query"][-1]
    cb(result)
    s.scheduler.fire_all()


class TestComposerComesBack(_SingleViewCase):
    def setUp(self):
        super().setUp()
        self.win = RecordingWindow()
        self.hv = HostView.for_window(self.win)

    def test_interrupt_while_typing_an_other_answer(self):
        s, out, client = _live(self.win, "a")
        self.hv.attach(self.win, s)
        s._enter_input_with_draft()
        out.set_composer_text("q")
        s.query("q")
        out.question_request(1, Q, lambda a: None)
        out.modals.handle_question_key("o")            # inline "Other…" input
        self.assertTrue(out.composer._question_input_mode)
        s.interrupt()
        _answer_bridge(s, client, {"status": "interrupted"})
        self.assertFalse(out.composer._question_input_mode)
        self.assertFalse(out.has_turn_modal_ui())
        self.assertTrue(out.is_input_mode(), "composer gone after interrupting a question")
        self.assertTrue(out.composer.input_marker_intact())

    def test_stale_question_input_flag_is_not_a_modal(self):
        s, out, _client = _live(self.win, "b")
        self.hv.attach(self.win, s)
        out.composer._question_input_mode = True        # left behind, no question pending
        self.assertFalse(out.has_turn_modal_ui())
        s._enter_input_with_draft()
        self.assertTrue(out.is_input_mode())

    def test_flag_without_marker_is_replanted(self):
        s, out, _client = _live(self.win, "c")
        self.hv.attach(self.win, s)
        s._enter_input_with_draft()
        # A rewrite dropped the ◎ line but the flag stayed on.
        v = out.view
        v.run_command("submarine_replace", {"start": 0, "end": v.size(), "text": "◎ old ▶\nreply\n"})
        self.assertTrue(out.is_input_mode())
        self.assertFalse(out.composer.input_marker_intact())
        s._enter_input_with_draft()
        self.assertTrue(out.composer.input_marker_intact(), "desynced composer flag was trusted")

    def test_outside_prompt_keeps_the_composer_and_the_draft(self):
        s, out, client = _live(self.win, "d")
        self.hv.attach(self.win, s)
        s._enter_input_with_draft()
        out.set_composer_text("my draft")
        s.query("from web", display_prompt="📨 from web")
        s.scheduler.fire_due(50)
        self.assertTrue(out.is_input_mode(), "composer gone during an outside turn")
        self.assertEqual(out.get_input_text(), "my draft")
        _answer_bridge(s, client, {"status": "ok"})
        self.assertTrue(out.is_input_mode())
        self.assertEqual(out.get_input_text(), "my draft")

    def test_question_answered_from_outside_while_viewless_then_attached(self):
        a, _ao, _ac = _live(self.win, "front")
        b, bo, bc = _live(self.win, "back")
        self.hv.attach(self.win, b)
        b._enter_input_with_draft()
        b.query("ask")
        bo.question_request(3, Q, lambda ans: None)     # composer hidden for the modal
        self.hv.attach(self.win, a)                       # b goes viewless with no composer in its snapshot
        bo.modals.answer_question("x")                    # answered from the web
        _answer_bridge(b, bc, {"status": "ok"})
        self.hv.attach(self.win, b)
        b.scheduler.fire_all()
        self.assertTrue(bo.is_input_mode(), "no composer after attaching a session answered viewless")
        self.assertTrue(bo.composer.input_marker_intact())



    def test_stale_sleeping_stamp_on_the_host_does_not_block_a_live_session(self):
        """Live evidence: view 20 had submarine_sleeping=true while its session
        was awake and working; every re-plant path bailed on the stamp."""
        from ui import keys
        s, out, _client = _live(self.win, "e")
        self.hv.attach(self.win, s)
        host = out.view
        keys.write_setting(host.settings(), keys.SLEEPING, True)   # left by a previous occupant
        self.assertFalse(s.is_sleeping)
        s._enter_input_with_draft()
        self.assertTrue(out.is_input_mode(), "composer blocked by a stale sleeping stamp")
        self.assertFalse(keys.read_setting(host.settings(), keys.SLEEPING, False))

    def test_attach_sets_the_sleeping_stamp_from_the_session(self):
        from ui import keys
        a, ao, _ac = _live(self.win, "awake")
        b, bo, _bc = _live(self.win, "asleep")
        b.client = None; b.initialized = False                     # sleeping
        self.hv.attach(self.win, b)
        host = bo.view
        self.assertTrue(keys.read_setting(host.settings(), keys.SLEEPING, False))
        self.hv.attach(self.win, a)
        self.assertFalse(keys.read_setting(host.settings(), keys.SLEEPING, False))
        self.assertTrue(ao.is_input_mode())


if __name__ == "__main__":
    unittest.main()
