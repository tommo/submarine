"""Switching into a sheet that waits on a question must leave it answerable.

The "Other…" input line is view state: `composer.detach()` dropped it when
the sheet went viewless, but the snapshot still painted the `▸ ` line back —
a dead prompt the user could not type into, Enter did nothing, and the
question keys were the only thing left working. The draft now survives the
detach and the line is rebuilt, caret at its end, on re-attach.
"""
from __future__ import annotations

import unittest

import ui.session_list as sl
from tests.stubs import install_sublime
from tests.test_composer_always_back import Q, _live
from tests.test_single_view import RecordingWindow, _SingleViewCase
from ui import keys
from ui.host import HostView


class TestQuestionSurvivesSwitch(_SingleViewCase):
    def setUp(self):
        super().setUp()
        install_sublime()          # commands.* import sublime at module level
        self.win = RecordingWindow()
        self.hv = HostView.for_window(self.win)

    def _switch_back(self, session):
        import commands.session_cmds as sc
        reveal = sc.SubmarineRevealSessionCommand.__new__(sc.SubmarineRevealSessionCommand)
        reveal.window = self.win
        self.assertTrue(reveal._reveal(session, self.win))
        sl.focus_sheet_soon(self.win, session)
        sl.reveal_tail_soon(session)
        session.scheduler.fire_all()

    def test_typing_an_other_answer_survives_a_switch(self):
        a, _ao, _ac = _live(self.win, "front")
        b, bo, _bc = _live(self.win, "asker")
        answered = []
        self.hv.attach(self.win, b)
        b._enter_input_with_draft()
        b.query("ask")
        bo.question_request(3, Q, lambda ans: answered.append(ans))
        bo.handle_question_key("o")
        bo.view.run_command("append", {"characters": "half typ"})
        self.hv.attach(self.win, a)                    # b goes viewless mid-answer
        a.scheduler.fire_all()
        self._switch_back(b)                           # Ctrl+] / Enter in the list
        view = bo.view
        c = bo.composer
        self.assertTrue(c._question_input_mode, "the ▸ input line is not live again")
        self.assertTrue(keys.read_setting(view.settings(), keys.QUESTION_INPUT_MODE, False))
        self.assertTrue(keys.read_setting(view.settings(), keys.INPUT_MODE, False))
        self.assertEqual(bo.modals.question_input_text(), "half typ")
        self.assertEqual(view.substr(None).count("▸"), 1, "a dead ▸ line was left behind")
        view.run_command("append", {"characters": "ed"})
        self.assertTrue(bo.modals.submit_question_input())
        self.assertEqual(answered, [{"pick": "half typed"}])

    def test_a_plain_question_is_still_answerable_after_a_switch(self):
        a, _ao, _ac = _live(self.win, "front")
        b, bo, _bc = _live(self.win, "asker")
        answered = []
        self.hv.attach(self.win, b)
        b._enter_input_with_draft()
        b.query("ask")
        bo.question_request(3, Q, lambda ans: answered.append(ans))
        self.hv.attach(self.win, a)
        a.scheduler.fire_all()
        self._switch_back(b)
        view = bo.view
        self.assertTrue(keys.read_setting(view.settings(), keys.HAS_QUESTION, False))
        self.assertIn("❓ pick", view.substr(None))
        self.assertNotIn("▸", view.substr(None))
        self.assertTrue(bo.handle_question_key("2"))
        self.assertEqual(answered, [{"pick": "y"}])


if __name__ == "__main__":
    unittest.main()
