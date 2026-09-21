"""Regression repros for the 2026-09-21 working-tree review.

Each test encodes the *expected* behaviour; a failure here confirms the
review finding it names.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from core.records import SessionStore
from core.registry import default_registry
from tests.fakes import make_session
from tests.test_single_view import (
    RecordingWindow,
    _SingleViewCase,
    _session,
)
from ui import keys
from ui.host import HostView


def _ask(session):
    session.output.question_request(
        "q1",
        [{"question": "Pick one", "options": [
            {"label": "a"}, {"label": "b"}]}],
        lambda *a, **k: None,
    )


class TestFinding1HasQuestionStamp(_SingleViewCase):
    """ui/modals.py:62 — has_question stamp must follow the bound session."""

    def test_swap_clears_stale_has_question_on_host(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        host = hv.host_view(win)
        _ask(a)
        self.assertTrue(keys.read_setting(host.settings(), keys.HAS_QUESTION, False))
        hv.attach(win, b)
        # B has no pending question; Enter/1-4/Esc must not route to
        # submarine_question_key / submarine_interrupt for B.
        self.assertFalse(
            keys.read_setting(host.settings(), keys.HAS_QUESTION, False),
            "stale has_question left on host after swapping A->B")
        self.assertFalse(
            keys.read_setting(host.settings(), keys.HAS_MODAL, False),
            "stale has_modal left on host after swapping A->B")

    def test_attach_stamps_has_question_for_viewless_pending(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        host = hv.host_view(win)
        self.assertIsNone(b.output.view)
        _ask(b)  # backgrounded: records request, no chrome
        self.assertIsNotNone(b.output.modals.pending_question)
        hv.attach(win, b)
        self.assertTrue(
            keys.read_setting(host.settings(), keys.HAS_QUESTION, False),
            "has_question not stamped when attaching a session with a pending question")


class TestFinding2RestoreLosesAgentLinks(unittest.TestCase):
    """ui/listeners.py:553 + main.py:920 — initial_context skips the resume
    branch that loads parent/child links; _save_session then persists the loss."""

    def _store(self):
        d = tempfile.mkdtemp(prefix="submarine-test-")
        path = os.path.join(d, ".sessions.json")
        row = {
            "session_id": "sess-child",
            "agent_id": "agent-child",
            "parent_agent_id": "agent-parent",
            "parent_session_id": "sess-parent",
            "child_agent_ids": ["agent-grandchild"],
            "agent_id_aliases": ["alias-1"],
            "query_count": 3,
            "total_cost": 1.25,
            "plan_file": "/tmp/plan.md",
            "state": "sleeping",
            "project": "/tmp/proj",
            "name": "child",
        }
        with open(path, "w") as f:
            json.dump([row], f)
        return SessionStore(path), row

    def _build(self, store, row, with_ctx):
        from ui.listeners import _hydrate_session_from_saved
        ctx = {"agent_id": row["agent_id"]} if with_ctx else None
        s = make_session(
            resume_id=row["session_id"], store=store,
            initial_context=ctx)
        s.store.path = store.path  # keep pointing at our fixture store
        _hydrate_session_from_saved(s, row)
        return s

    def test_control_resume_without_ctx_keeps_links(self):
        store, row = self._store()
        s = self._build(store, row, with_ctx=False)
        self.assertEqual(s.agent_id, "agent-child")
        self.assertEqual(s.parent_agent_id, "agent-parent")
        self.assertEqual(s.child_agent_ids, ["agent-grandchild"])

    def test_restore_path_keeps_parent_child_links_in_memory(self):
        store, row = self._store()
        s = self._build(store, row, with_ctx=True)  # as listeners.py:553 does
        self.assertEqual(s.agent_id, "agent-child")
        self.assertEqual(s.parent_agent_id, "agent-parent",
                         "parent_agent_id dropped by initial_context branch")
        self.assertEqual(s.child_agent_ids, ["agent-grandchild"])
        self.assertEqual(s.agent_id_aliases, ["alias-1"])

    def test_plugin_unloaded_save_does_not_strip_saved_row(self):
        store, row = self._store()
        s = self._build(store, row, with_ctx=True)
        s._save_session()  # as main.py:920 plugin_unloaded does
        saved = store.find("sess-child")
        self.assertIsNotNone(saved)
        for k in ("parent_agent_id", "child_agent_ids", "agent_id_aliases",
                  "total_cost", "plan_file"):
            self.assertEqual(
                saved.get(k), row[k],
                "%s lost from .sessions.json after restore+save (was %r, now %r)"
                % (k, row[k], saved.get(k)))


class TestFinding3HandoffOnViewlessDismiss(_SingleViewCase):
    """ui/host.py:472 — dismissing a viewless non-bound session must not
    swap the host away from the session the user is looking at."""

    def test_dismiss_backgrounded_peer_leaves_host_alone(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        c = _session(win, "C")
        for i, s in enumerate((b, a, c)):
            s.last_access = 100.0 + i  # c newest, then a, then b
            default_registry.register_session(s)
        hv = HostView.for_window(win)
        hv.attach(win, a)
        self.assertIs(hv.bound_session(win), a)
        self.assertIsNone(b.output.view)
        handed = hv.handoff_host_on_dismiss(win, b)
        self.assertFalse(handed, "handoff reported for a session that wasn't bound")
        self.assertIs(hv.bound_session(win), a,
                      "host swapped away from A when dismissing viewless B "
                      "(now showing %s)" % getattr(hv.bound_session(win), "name", None))
        self.assertFalse(getattr(b, "stopped", False))


class TestFinding5HideStopsSleeping(_SingleViewCase):
    """commands/session_cmds.py:464 — Hide is detach-only; a sleeping bound
    session must not be stop()ped by the handoff path."""

    def test_handoff_without_stop_flag_never_stops_sleeping_session(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        default_registry.register_session(b)
        hv = HostView.for_window(win)
        hv.attach(win, a)
        # Make A sleeping: session_id set, no client, not initialized.
        a.client = None
        a.initialized = False
        self.assertTrue(a.is_sleeping)
        calls = []
        a.stop = lambda *x, **k: calls.append(("stop", x, k))
        hv.handoff_host_on_dismiss(win, a)  # stop=False, as Hide Session calls it
        self.assertEqual(calls, [], "Hide Session path called stop() on a sleeping session")


class TestFinding6QuestionDoesNotStealFocus(_SingleViewCase):
    """ui/modals.py:909 — question_request must respect show(focus=False)."""

    def test_question_leaves_the_active_view_alone(self):
        win = RecordingWindow()
        a = _session(win, "A")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        editor = win.new_file()  # user is typing in another tab
        win.focus_view(editor)
        _ask(a)
        self.assertIs(win.active_view(), editor,
                      "question_request stole focus from the editor tab")


class TestFinding7TornOffRevealFocuses(_SingleViewCase):
    """ui/session_list.py:1340 — clicking a torn-off row must reveal its sheet
    even when the view is valid but hidden behind another tab."""

    def test_torn_off_row_focuses_hidden_sheet(self):
        from ui.session_list import reveal_row

        win = RecordingWindow()
        host_session = _session(win, "host")
        hv = HostView.for_window(win)
        hv.attach(win, host_session)
        torn = _session(win, "torn")
        torn.torn_off = True
        sheet = win.new_file()  # torn-off sheet, currently a background tab
        torn.output.view = sheet
        torn.output.window = win
        default_registry.register_session(torn)
        other = win.new_file()
        win.focus_view(other)
        row = {"kind": "live", "session_id": torn.session_id,
               "agent_id": torn.agent_id, "torn_off": True}
        seen = []
        real_focus = win.focus_view

        def _focus(v):
            seen.append(v)
            real_focus(v)
        win.focus_view = _focus
        self.assertTrue(reveal_row(win, row, keep=other))
        self.assertIn(sheet, seen, "torn-off sheet was never focused/revealed")


class TestFinding8SpinnerInsertShiftsComposer(unittest.TestCase):
    """ui/renderer.py:801 — inserting the first glyph before the sticky
    composer must shift its anchors and grow (not shrink) the region."""

    def test_insert_path_shifts_anchors_and_grows_region(self):
        from ui.models import Conversation
        from ui.renderer import TurnRenderer
        from tests.test_single_view import RecordingView, _Region

        conv = "◎ hi ▶\n"
        composer = "◎ draft"
        shifts = []

        class _Composer(object):
            _question_input_mode = False

            def __init__(self):
                self._input_start = len(conv)

            def is_input_mode(self):
                return True

            def peel_start(self):
                return len(conv)

            def shift_anchors(self, d):
                shifts.append(d)
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
                self.view._content = conv + composer
                self.view._regions = {keys.CONV_REGION: [_Region(0, len(conv))]}
                self.sheet = _Sheet(self.view)
                self.composer = _Composer()
                self.modals = None

            def has_turn_modal_ui(self):
                return False

            def _replace(self, start, end, text):
                v = self.view
                v._content = v._content[:start] + text + v._content[end:]
                return start + len(text)

        owner = _Owner()
        r = TurnRenderer(owner)
        r.current = Conversation(prompt="q", working=True)
        r.current.region = (0, len(conv))
        self.assertTrue(r._patch_spinner_glyph("◇"))
        glyph = "  ◇\n"
        self.assertEqual(owner.view._content, conv + glyph + composer)
        self.assertEqual(shifts, [len(glyph)],
                         "composer anchors not shifted past the inserted glyph line")
        self.assertEqual(owner.composer._input_start, len(conv) + len(glyph))
        self.assertEqual(r.current.region, (0, len(conv) + len(glyph)))
        tracked = owner.view._regions[keys.CONV_REGION][0]
        self.assertEqual((tracked.begin(), tracked.end()), (0, len(conv) + len(glyph)))

    def test_insert_path_never_shrinks_region_when_end_is_clamped(self):
        from ui.models import Conversation
        from ui.renderer import TurnRenderer
        from tests.test_single_view import RecordingView, _Region

        conv = "◎ hi ▶\n"
        trail = "  ❓ pick\n"

        class _Modals(object):
            def trailing_ui_start(self):
                return len(conv)

        class _Sheet(object):
            def __init__(self, view):
                self.view = view

            def set_hidden_region(self, key, a, b):
                self.view._regions[key] = [_Region(a, b)]

        class _Owner(object):
            def __init__(self):
                self.view = RecordingView()
                self.view._content = conv + trail
                # Tracked region spans conv + trail (as the renderer left it).
                self.view._regions = {
                    keys.CONV_REGION: [_Region(0, len(conv) + len(trail))]}
                self.sheet = _Sheet(self.view)
                self.composer = None
                self.modals = _Modals()

            def has_turn_modal_ui(self):
                return False

            def _replace(self, start, end, text):
                v = self.view
                v._content = v._content[:start] + text + v._content[end:]
                return start + len(text)

        owner = _Owner()
        r = TurnRenderer(owner)
        r.current = Conversation(prompt="q", working=True)
        r.current.region = (0, len(conv) + len(trail))
        self.assertTrue(r._patch_spinner_glyph("◇"))
        glyph = "  ◇\n"
        # Region grew by the glyph; it was not cut back to the clamped end.
        self.assertEqual(r.current.region, (0, len(conv) + len(trail) + len(glyph)))


if __name__ == "__main__":
    unittest.main()
