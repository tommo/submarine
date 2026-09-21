"""Single-view default + tear-off / dock of host sessions."""
from __future__ import annotations

import os
import unittest

from core.registry import default_registry
from tests.test_single_view import RecordingWindow, _session
from ui import idle
from ui.host import (
    HostView,
    apply_ui_mode,
    can_dock,
    can_tear_off,
    is_single_mode,
    set_ui_mode_override,
    ui_mode,
)
from ui.session_api import get_session_for_view
from ui.session_list import (
    ROWS_KEY,
    SETTING,
    _fmt_row,
    collect_live,
    follow_current_under_caret,
    open_row,
    tear_or_dock_row,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _TearOffCase(unittest.TestCase):
    def setUp(self):
        default_registry.clear()
        HostView.reset()
        set_ui_mode_override("single")

    def tearDown(self):
        default_registry.clear()
        HostView.reset()


class TestDefaultFlip(unittest.TestCase):
    def tearDown(self):
        HostView.reset()

    def test_settings_default_is_single(self):
        path = os.path.join(ROOT, "Submarine.sublime-settings")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn('"ui_mode": "single"', text)
        self.assertNotIn('"ui_mode": "tabs"', text)

    def test_no_override_is_single(self):
        HostView.reset()
        self.assertEqual(ui_mode(), "single")
        self.assertTrue(is_single_mode())

    def test_stub_load_settings_without_key_is_single(self):
        import ui.host as host

        class _S(object):
            def get(self, k, d=None):
                return d

        class _Sub(object):
            def load_settings(self, name):
                return _S()

        saved = host.sublime
        try:
            host.sublime = _Sub()
            host.set_ui_mode_override(None)
            self.assertEqual(host.ui_mode(), "single")
            self.assertTrue(host.is_single_mode())
        finally:
            host.sublime = saved
            HostView.reset()


class TestTearOff(_TearOffCase):
    def test_bound_session_gets_own_sheet_and_host_reattaches(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        a.last_access = 10
        b.last_access = 20
        hv = HostView.for_window(win)
        self.assertTrue(hv.attach(win, a))
        self.assertTrue(hv.attach(win, b))
        host = hv.host_view(win)
        self.assertIs(b.output.view, host)
        self.assertTrue(can_tear_off(win))
        self.assertTrue(hv.tear_off(win, b))
        self.assertTrue(b.torn_off)
        self.assertIsNotNone(b.output.view)
        self.assertTrue(b.output.view.is_valid())
        self.assertIsNot(b.output.view, host)
        self.assertTrue(host.is_valid())
        self.assertIs(a.output.view, host)
        self.assertIs(default_registry.for_view(host), a)
        self.assertIs(get_session_for_view(b.output.view), b)
        self.assertIs(get_session_for_view(host), a)
        self.assertFalse(can_dock(win, a))
        win.focus_view(b.output.view)
        self.assertTrue(can_dock(win))
        self.assertFalse(hv.attach(win, b))

    def test_tear_off_alone_writes_placeholder(self):
        win = RecordingWindow()
        a = _session(win, "A")
        hv = HostView.for_window(win)
        self.assertTrue(hv.attach(win, a))
        host = hv.host_view(win)
        self.assertTrue(hv.tear_off(win, a))
        self.assertTrue(a.torn_off)
        self.assertIsNot(a.output.view, host)
        self.assertIsNone(default_registry.for_view(host))
        self.assertIn(idle.BRAND, host._content, "the idle page")
        self.assertTrue(host.is_valid())

    def test_tear_off_without_session_uses_bound(self):
        win = RecordingWindow()
        a = _session(win, "A")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        self.assertTrue(hv.tear_off(win))
        self.assertTrue(a.torn_off)


class TestDock(_TearOffCase):
    def test_dock_returns_to_host_and_closes_sheet(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        a.last_access = 1
        b.last_access = 2
        hv = HostView.for_window(win)
        hv.attach(win, a)
        hv.attach(win, b)
        host = hv.host_view(win)
        self.assertTrue(hv.tear_off(win, b))
        torn = b.output.view
        self.assertIsNot(torn, host)
        self.assertTrue(hv.dock(win, b))
        self.assertFalse(b.torn_off)
        self.assertIs(b.output.view, host)
        self.assertTrue(torn.closed)
        self.assertFalse(torn.is_valid())
        self.assertIs(default_registry.for_view(host), b)
        self.assertIsNone(a.output.view)
        self.assertFalse(can_dock(win, b))


class TestSessionList(_TearOffCase):
    def test_torn_off_row_opens_own_sheet_and_shows_marker(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        a.last_access = 1
        b.last_access = 2
        hv = HostView.for_window(win)
        hv.attach(win, a)
        hv.attach(win, b)
        host = hv.host_view(win)
        self.assertTrue(hv.tear_off(win, b))
        torn = b.output.view
        rows = collect_live(win)
        torn_rows = [r for r in rows if r.get("torn_off")]
        bound_rows = [r for r in rows if r.get("bound")]
        self.assertEqual(len(torn_rows), 1)
        self.assertEqual(torn_rows[0]["session_id"], b.session_id)
        self.assertFalse(torn_rows[0].get("bound"))
        self.assertEqual(len(bound_rows), 1)
        self.assertEqual(bound_rows[0]["session_id"], a.session_id)
        mark = _fmt_row(torn_rows[0], set(), False, 80)
        # Current-session column first (`▸ ` bound, else `  `), then the mark.
        self.assertEqual(mark[:2], "  ")
        self.assertEqual(mark[2], "⊡")
        self.assertTrue(open_row(win, torn_rows[0]))
        self.assertIs(b.output.view, torn)
        self.assertIsNot(b.output.view, host)
        self.assertIs(get_session_for_view(b.output.view), b)
        self.assertIs(a.output.view, host)

    def test_open_bound_row_focuses_host_without_reattach(self):
        win = RecordingWindow()
        a = _session(win, "FocusA")
        b = _session(win, "FocusB")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        hv.attach(win, b)
        host = hv.host_view(win)
        list_view = win.new_file()
        win.focus_view(list_view)
        rows = collect_live(win)
        bound = [r for r in rows if r.get("bound")][0]
        self.assertEqual(bound["session_id"], b.session_id)
        ops = host.buffer_ops
        self.assertTrue(open_row(win, bound))
        self.assertIs(win.active_view(), host)
        self.assertIs(b.output.view, host)
        self.assertIsNone(a.output.view)
        self.assertEqual(host.buffer_ops, ops)

    def test_open_sleeping_row_wakes(self):
        win = RecordingWindow()
        a = _session(win, "WakeA")
        b = _session(win, "WakeB")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        hv.attach(win, b)
        host = hv.host_view(win)
        b.client = None
        b.initialized = False
        self.assertTrue(b.is_sleeping)
        woken = []
        b.wake = lambda: woken.append(1)
        list_view = win.new_file()
        win.focus_view(list_view)
        rows = collect_live(win)
        bound = [r for r in rows if r.get("bound")][0]
        self.assertEqual(bound["session_id"], b.session_id)
        self.assertTrue(open_row(win, bound))
        self.assertEqual(woken, [1])
        self.assertIs(win.active_view(), host)

    def test_caret_on_current_row_swaps_host_keeps_list_focus(self):
        import json
        from tests.test_single_view import _Region, _Settings

        win = RecordingWindow()
        a = _session(win, "CaretA")
        b = _session(win, "CaretB")
        a.last_access = 1
        b.last_access = 2
        hv = HostView.for_window(win)
        hv.attach(win, a)
        hv.attach(win, b)
        hv.attach(win, a)
        host = hv.host_view(win)
        rows = collect_live(win)
        for i, r in enumerate(rows):
            r["line"] = i + 4
            r["section"] = "CURRENT"
        b_row = [r for r in rows if r["session_id"] == b.session_id][0]
        a_row = [r for r in rows if r["session_id"] == a.session_id][0]

        class _ListView(object):
            def __init__(self, line):
                self._settings = _Settings()
                self._settings.set(SETTING, True)
                self._settings.set(ROWS_KEY, json.dumps(rows))
                self._line = line
                self._sel = [_Region(0)]

            def is_valid(self):
                return True

            def settings(self):
                return self._settings

            def window(self):
                return win

            def sel(self):
                return self._sel

            def rowcol(self, pt):
                return (self._line - 1, 0)

            def id(self):
                return 4242 + self._line

        lv = _ListView(b_row["line"])

        class _Proxy(object):
            def __init__(self, inner):
                self._inner = inner

            def id(self):
                return self._inner.id()

            def is_valid(self):
                return True

            def settings(self):
                return self._inner.settings()

            def window(self):
                return win

            def sel(self):
                return self._inner.sel()

            def rowcol(self, pt):
                return self._inner.rowcol(pt)

        win.focus_view(_Proxy(lv))
        self.assertTrue(follow_current_under_caret(lv))
        self.assertIs(b.output.view, host)
        self.assertIsNone(a.output.view)
        self.assertEqual(win.active_view().id(), lv.id())

        lv_a = _ListView(a_row["line"])
        win.focus_view(lv_a)
        self.assertTrue(follow_current_under_caret(lv_a))
        self.assertIs(a.output.view, host)
        self.assertIs(win.active_view(), lv_a)

        hist = dict(b_row)
        hist["kind"] = "saved"
        hist["section"] = "HISTORY"
        hist["line"] = 20
        lv_h = _ListView(20)
        lv_h.settings().set(ROWS_KEY, json.dumps(rows + [hist]))
        win.focus_view(lv_h)
        self.assertFalse(follow_current_under_caret(lv_h))
        self.assertIs(a.output.view, host)

    def test_caret_follow_does_not_wake_sleeping(self):
        import json
        from tests.test_single_view import _Region, _Settings
        from ui.session_list import follow_current_under_caret, SETTING, ROWS_KEY

        win = RecordingWindow()
        a = _session(win, "NoWakeA")
        b = _session(win, "NoWakeB")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        hv.attach(win, b)
        b.client = None
        b.initialized = False
        woken = []
        b.wake = lambda: woken.append(1)
        rows = collect_live(win)
        for i, r in enumerate(rows):
            r["line"] = i + 4
            r["section"] = "CURRENT"
        b_row = [r for r in rows if r["session_id"] == b.session_id][0]

        class _ListView(object):
            def __init__(self):
                self._settings = _Settings()
                self._settings.set(SETTING, True)
                self._settings.set(ROWS_KEY, json.dumps(rows))
                self._sel = [_Region(0)]

            def is_valid(self):
                return True

            def settings(self):
                return self._settings

            def window(self):
                return win

            def sel(self):
                return self._sel

            def rowcol(self, pt):
                return (b_row["line"] - 1, 0)

            def id(self):
                return 9090

        lv = _ListView()
        win.focus_view(lv)
        self.assertTrue(follow_current_under_caret(lv))
        self.assertEqual(woken, [])

    def test_click_into_list_follows_caret_line_while_host_is_active(self):
        import json
        from tests.test_single_view import _Region, _Settings
        from ui.session_list import follow_current_under_caret, SETTING, ROWS_KEY

        win = RecordingWindow()
        a = _session(win, "ClickA")
        b = _session(win, "ClickB")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        hv.attach(win, b)
        hv.attach(win, a)
        host = hv.host_view(win)
        win.focus_view(host)
        rows = collect_live(win)
        for i, r in enumerate(rows):
            r["line"] = i + 4
            r["section"] = "CURRENT"
        b_row = [r for r in rows if r["session_id"] == b.session_id][0]

        class _ListView(object):
            def __init__(self):
                self._settings = _Settings()
                self._settings.set(SETTING, True)
                self._settings.set(ROWS_KEY, json.dumps(rows))
                self._sel = [_Region(0)]

            def is_valid(self):
                return True

            def settings(self):
                return self._settings

            def window(self):
                return win

            def sel(self):
                return self._sel

            def rowcol(self, pt):
                return (b_row["line"] - 1, 0)

            def id(self):
                return 7070

        lv = _ListView()
        self.assertIs(win.active_view(), host)
        self.assertFalse(follow_current_under_caret(lv))
        self.assertIs(a.output.view, host)
        self.assertTrue(follow_current_under_caret(lv, force=True))
        self.assertIs(b.output.view, host)
        self.assertIs(win.active_view(), lv)

    def test_follow_already_bound_does_not_refocus_the_host(self):
        import json
        from tests.test_single_view import _Region, _Settings
        from ui.session_list import follow_current_under_caret, SETTING, ROWS_KEY

        win = RecordingWindow()
        a = _session(win, "StayA")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        host = hv.host_view(win)
        rows = collect_live(win)
        for i, r in enumerate(rows):
            r["line"] = i + 4
            r["section"] = "CURRENT"
        a_row = [r for r in rows if r["session_id"] == a.session_id][0]

        class _ListView(object):
            def __init__(self):
                self._settings = _Settings()
                self._settings.set(SETTING, True)
                self._settings.set(ROWS_KEY, json.dumps(rows))
                self._sel = [_Region(0)]

            def is_valid(self):
                return True

            def settings(self):
                return self._settings

            def window(self):
                return win

            def sel(self):
                return self._sel

            def rowcol(self, pt):
                return (a_row["line"] - 1, 0)

            def id(self):
                return 6161

        lv = _ListView()
        win.focus_view(lv)
        focused = []
        real = win.focus_view

        def _focus(v):
            focused.append(getattr(v, "id", lambda: None)())
            return real(v)

        win.focus_view = _focus
        focused.clear()
        self.assertTrue(follow_current_under_caret(lv))
        self.assertIs(hv.bound_session(win), a)
        self.assertNotIn(host.id(), focused)

    def test_list_key_tears_off_bound_and_docks_torn_off(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        a.last_access = 1
        b.last_access = 2
        hv = HostView.for_window(win)
        hv.attach(win, a)
        hv.attach(win, b)
        host = hv.host_view(win)
        rows = collect_live(win)
        bound = [r for r in rows if r.get("bound")][0]
        self.assertTrue(tear_or_dock_row(win, bound))
        self.assertTrue(b.torn_off)
        self.assertIsNot(b.output.view, host)
        rows = collect_live(win)
        torn = [r for r in rows if r.get("torn_off")][0]
        self.assertTrue(tear_or_dock_row(win, torn))
        self.assertFalse(b.torn_off)
        self.assertIs(b.output.view, host)


class TestTabsModeNoOp(_TearOffCase):
    def test_tear_off_and_dock_are_nops(self):
        set_ui_mode_override("tabs")
        win = RecordingWindow()
        a = _session(win, "A")
        a.output.show(focus=False, create=True)
        default_registry.register_session(a)
        hv = HostView.for_window(win)
        self.assertFalse(can_tear_off(win))
        self.assertFalse(can_dock(win, a))
        self.assertFalse(hv.tear_off(win, a))
        self.assertFalse(a.torn_off)
        a.torn_off = True
        self.assertFalse(hv.dock(win, a))
        self.assertTrue(a.torn_off)
        self.assertTrue(a.output.view.is_valid())


class TestKeepAlive(_TearOffCase):
    def test_busy_tear_off_does_not_stop_and_sheet_shows_content(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        a.last_access = 1
        b.last_access = 2
        hv = HostView.for_window(win)
        hv.attach(win, a)
        a.output.prompt("hello")
        a.output.text("live body")
        a.turn.begin_query()
        client = a.client
        self.assertTrue(a.working)
        hv.attach(win, b)
        hv.attach(win, a)
        self.assertTrue(hv.tear_off(win, a))
        self.assertTrue(a.working)
        self.assertIs(a.client, client)
        self.assertFalse(getattr(a, "stopped", False))
        self.assertTrue(a.torn_off)
        sheet = a.output.view
        self.assertIsNotNone(sheet)
        self.assertIn("hello", sheet._content)
        self.assertIn("live body", sheet._content)
        self.assertIs(get_session_for_view(sheet), a)


class TestModeSwitchClearsTornOff(_TearOffCase):
    def test_single_to_tabs_clears_marks(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        hv.attach(win, b)
        self.assertTrue(hv.tear_off(win, b))
        self.assertTrue(b.torn_off)
        apply_ui_mode(win, "tabs")
        self.assertFalse(b.torn_off)
        self.assertFalse(a.torn_off)

    def test_closing_torn_off_sheet_keeps_flag(self):
        win = RecordingWindow()
        a = _session(win, "A")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        host = hv.host_view(win)
        self.assertTrue(hv.tear_off(win, a))
        sheet = a.output.view
        default_registry.detach_session(a)
        sheet.close()
        self.assertTrue(a.torn_off)
        self.assertIsNone(a.output.view)
        from ui.session_list import reveal_live_session
        self.assertTrue(reveal_live_session(win, a, focus=True))
        self.assertTrue(a.torn_off)
        self.assertIsNotNone(a.output.view)
        self.assertTrue(a.output.view.is_valid())
        self.assertIsNot(a.output.view, host)


if __name__ == "__main__":
    unittest.main()
