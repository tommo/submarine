"""Session list scratch: render + line index (no Sublime runtime)."""
from __future__ import annotations

import json
import os
import tempfile
import time
import types
import unittest

from ui import session_list as sl

MARK_CHARS = ("○", "●", "?", "!", "⏸", "⊡", "·")


def _live(sid, name, **kw):
    row = {
        "kind": "live",
        "session_id": sid,
        "agent_id": kw.pop("aid", sid),
        "parent_agent_id": kw.pop("parent", None),
        "view_id": kw.pop("view_id", None),
        "name": name,
        "backend": kw.pop("backend", "grok"),
        "status": kw.pop("status", "ready"),
        "query_count": kw.pop("queries", 1),
        "same_window": True,
        "last_access": kw.pop("access", 1),
        "last_activity": kw.pop("activity", None),
    }
    if row["last_activity"] is None:
        row["last_activity"] = row["last_access"]
    row.update(kw)
    return row


def _saved(sid, name, **kw):
    row = _live(sid, name, **kw)
    row["kind"] = "saved"
    row["status"] = kw.get("status", "closed")
    row["view_id"] = None
    return row


def _row_cells(line):
    """(prefix, state mark) of a rendered row, or ("", "") for headers.

    A live row opens with the current-session column (`▸ ` or `  `), a saved row
    with its mark; the nesting glyph sits after the mark, and a pinned row's
    sparkle sits past the backend column, in front of the title.
    """
    text = line or ""
    lead = ""
    if text[:1] == sl.CUR_MARK and text[1:2] in MARK_CHARS:
        lead, text = text[:1], text[1:]  # the live row's current-session column
    for i, ch in enumerate(text[:8]):
        if ch in MARK_CHARS:
            return lead + text[:i], ch
    return "", ""


def _row_mark(line):
    return _row_cells(line)[1]


def _row_lead(line):
    return _row_cells(line)[0]


def _session_lines(text):
    return [ln for ln in text.splitlines() if _row_mark(ln)]


class TestIdlePoll(unittest.TestCase):
    """A poll with nothing to show must not rebuild the Sessions view.

    A tick compares a fingerprint (registry + two store stats) and only renders
    when that or the elapsed-time column changed, then backs off while idle.
    """

    def setUp(self):
        self._orig = (dict(sl._fingerprints), dict(sl._stamps), sl._poll_delay,
                      sl._poll_armed, sl._poll_stopped, sl._poll_gen)
        self._paths = (sl._sessions_store_path, sl.load_bookmarks,
                       sl.load_bookmark_records)
        self._list_cls = sl.SessionListView
        self._td = tempfile.TemporaryDirectory(prefix="submarine-poll-")
        sl._sessions_store_path = lambda: os.path.join(self._td.name, "sessions.json")
        self._py = None
        self._gen = None
        self._sublime = None
        self._bound_sublime = False

    def tearDown(self):
        sl._fingerprints.clear()
        sl._fingerprints.update(self._orig[0])
        sl._stamps.clear()
        sl._stamps.update(self._orig[1])
        (sl._poll_delay, sl._poll_armed,
         sl._poll_stopped, sl._poll_gen) = self._orig[2:]
        (sl._sessions_store_path, sl.load_bookmarks,
         sl.load_bookmark_records) = self._paths
        sl.SessionListView = self._list_cls
        if self._sublime is not None and self._py is not None:
            (self._sublime.windows, self._sublime.set_timeout,
             self._sublime._claude_sessions) = self._py
        if self._gen is not None:
            mod, val = self._gen
            if val is None:
                try:
                    delattr(mod, "_submarine_poll_gen")
                except AttributeError:
                    pass
            else:
                mod._submarine_poll_gen = val
        if self._bound_sublime:
            sl.sublime = None
        self._td.cleanup()
        import sys
        sm = sys.modules.get("sublime")
        if sm is not None and getattr(sm, "_submarine_stub", False):
            sys.modules.pop("sublime", None)
            sys.modules.pop("sublime_plugin", None)

    def test_stamp_boundary_matches_format_when(self):
        now = 1800000000.0
        for secs in (0, 4, 5, 6, 59, 60, 61, 3599, 3600, 86399, 86400,
                     86400 * 14 - 1, 86400 * 14, 86400 * 30):
            ts = now - secs
            label = sl.format_when(ts, now=now)
            step = sl._stamp_change_in(ts, now)
            self.assertIsNotNone(step, secs)
            self.assertGreater(step, 0, secs)
            self.assertEqual(sl.format_when(ts, now=now + step - 1), label, secs)
            self.assertNotEqual(sl.format_when(ts, now=now + step), label, secs)
        self.assertIsNone(sl._stamp_change_in(0, now))
        self.assertIsNone(sl._stamp_change_in(None, now))

    def test_next_stamp_change_uses_the_soonest_clock_row(self):
        now = 1800000000.0
        # A live row shows a state word, not a clock: it schedules no tick.
        state = {"kind": "live", "status": "ready", "last_access": now - 99999}
        self.assertEqual(sl._next_stamp_change([state], now=now), 0.0)
        hist = {"kind": "saved", "status": "closed", "last_access": now - 10}
        self.assertEqual(sl._next_stamp_change([state, hist], now=now), now + 50)
        self.assertEqual(sl._next_stamp_change([], now=now), 0.0)

    def test_poll_delay_backs_off_but_wakes_for_a_stamp(self):
        _win, view = self._fake_list_window()
        sl._stamps.clear()
        sl._poll_delay = sl._POLL_MIN_MS
        self.assertEqual(sl._next_poll_delay(), sl._POLL_MIN_MS)
        sl._poll_delay = sl._POLL_MAX_MS
        self.assertEqual(sl._next_poll_delay(), sl._POLL_MAX_MS)
        sl._stamps[view.id()] = time.time() + 2
        self.assertLessEqual(sl._next_poll_delay(), 2100)
        sl._stamps[view.id()] = time.time() - 5  # already due: poll now
        self.assertEqual(sl._next_poll_delay(), sl._POLL_MIN_MS)

    def test_state_key_covers_sessions_cols_stores_and_stars(self):
        sublime = self._stub()
        win = _win_for(["proj/here"])
        session = _live_session(win, "s1")
        sublime._claude_sessions = {1: session}
        sess = sl._sessions_store_path()
        key = sl.list_state_key(win, 80)
        self.assertEqual(key, sl.list_state_key(win, 80))
        self.assertNotEqual(key, sl.list_state_key(win, 60))
        session.working = True
        self.assertNotEqual(key, sl.list_state_key(win, 80))
        key = sl.list_state_key(win, 80)
        with open(sess, "w", encoding="utf-8") as f:
            f.write("[]")
        self.assertNotEqual(key, sl.list_state_key(win, 80))
        key = sl.list_state_key(win, 80)
        sl.load_bookmarks = lambda project_path=None: {"s1"}
        self.assertNotEqual(key, sl.list_state_key(win, 80))
        key = sl.list_state_key(win, 80)
        sl.load_bookmark_records = lambda project_path=None: {"s1": {"name": "n"}}
        self.assertNotEqual(key, sl.list_state_key(win, 80))

    def test_schedule_resets_backoff_and_is_snappy(self):
        self._fake_list_window()
        sl._poll_delay = sl._POLL_MAX_MS
        sl._poll_armed = True
        sl._refresh_pending = False
        sl.schedule_session_list_refresh()
        self.assertEqual(sl._poll_delay, sl._POLL_MIN_MS)
        self.assertEqual(self._timeouts, [50])
        sl.schedule_session_list_refresh()
        self.assertEqual(self._timeouts, [50], "debounce coalesces")

    def test_idle_tick_rebuilds_nothing_and_backs_off(self):
        _win, view = self._fake_list_window()
        sl._stamps.clear()
        sl._poll_delay = sl._POLL_MIN_MS
        sl._poll_armed = True
        sl._session_list_poll()
        self.assertEqual(self._timeouts, [sl._POLL_MIN_MS * 2])
        self.assertEqual(self._renders, [])
        self.assertEqual(sl._poll_delay, sl._POLL_MIN_MS * 2)
        # A due elapsed-time column is the one thing a quiet list polls for.
        self._timeouts[:] = []
        sl._stamps[view.id()] = time.time() - 1
        sl._session_list_poll()
        self.assertEqual(len(self._renders), 1)
        self.assertEqual(sl._poll_delay, sl._POLL_MIN_MS)

    def test_one_list_does_not_starve_anothers_clock(self):
        """Each list owns its own deadline: a list with old rows re-arming a
        far-off one used to freeze the other list's clock for good."""
        wins, views = self._fake_list_windows(2)
        now = time.time()
        sl._stamps.clear()
        sl._stamps[views[0].id()] = now + 3600   # fresh rows: nothing due
        sl._stamps[views[1].id()] = now - 1      # this one wants a repaint
        sl._poll_delay = sl._POLL_MAX_MS
        sl._poll_armed = True
        sl._session_list_poll()
        self.assertEqual(self._renders, [views[1].id()])
        # And the soonest deadline, not the newest, is what the poll wakes for.
        self.assertEqual(sl._soonest_stamp(), now - 1)

    def test_closed_list_view_stops_the_chain(self):
        win, _view = self._fake_list_window()
        win._views = []
        sl._poll_armed = True
        sl._session_list_poll()
        self.assertEqual(self._timeouts, [])
        self.assertFalse(sl._poll_armed)

    def test_unload_stops_this_incarnations_chain(self):
        self._fake_list_window()
        sl._poll_armed = True
        sl._poll_stopped = False
        sl.stop_session_list_poll()
        sl._session_list_poll()
        self.assertEqual(self._timeouts, [])
        self.assertFalse(sl._poll_armed)
        self.assertTrue(sl._poll_stopped)

    def test_a_reload_also_stops_the_other_copies(self):
        """Unload runs in whichever copy is current; the shared counter is how
        a chain left behind by an earlier copy learns to stop."""
        sublime = self._stub()
        self._fake_list_window()
        sl._poll_armed = True
        sl._poll_gen = sl._poll_generation()
        sublime._submarine_poll_gen = sl._poll_generation() + 1
        sl._session_list_poll()
        self.assertEqual(self._timeouts, [])
        self.assertFalse(sl._poll_armed)

    # -- fixtures ----------------------------------------------------------
    def _stub(self, sublime=None):
        if sublime is None:
            from tests.stubs import install
            sublime = install()
        self._sublime = sublime
        if self._py is None:
            self._py = (sublime.windows, sublime.set_timeout,
                        sublime._claude_sessions)
            self._gen = (sublime, getattr(sublime, "_submarine_poll_gen", None))
        if getattr(sl, "sublime", None) is None:
            # `ui.session_list` binds sublime at import, which pytest has not
            # installed yet; the poll path needs it.
            sl.sublime = sublime
            self._bound_sublime = True
        return sublime

    def _fake_list_window(self):
        """A window holding one Sessions view, with timeouts and renders taped."""
        win, view = self._fake_list_windows(1)
        return win[0], view[0]

    def _fake_list_windows(self, count):
        """`count` windows, each with its own Sessions view, all taped."""
        from tests.stubs import FakeView, FakeWindow
        sublime = self._stub()
        wins, views = [], []
        for i in range(count):
            view = FakeView(7 + i)
            view.settings().set(sl.SETTING, True)
            win = FakeWindow()
            win._views.append(view)
            wins.append(win)
            views.append(view)
        self._timeouts = []
        self._renders = []
        sublime.windows = lambda: list(wins)
        sublime.set_timeout = lambda f, t=0: self._timeouts.append(t)
        sublime._claude_sessions = {}
        for win, view in zip(wins, views):
            sl._fingerprints[view.id()] = sl.list_state_key(win, sl.view_cols(view))

        class _RecordingList(object):
            _renders = self._renders

            def refresh(self, follow=False):
                self._renders.append(self.view.id())
        # The list builds a view-bound instance with `__new__`, then sets it up.
        sl.SessionListView = _RecordingList
        return wins, views


def _win_for(folders):
    """A window stand-in for `collect_live` (only `folders()` is read)."""
    class _Win:
        def folders(self):
            return list(folders)
    return _Win()


def _live_session(win, sid, **kw):
    return types.SimpleNamespace(
        session_id=sid, name=kw.pop("name", sid), backend=kw.pop("backend", "grok"),
        working=kw.pop("working", False), is_sleeping=False,
        query_count=kw.pop("queries", 1), last_activity=1, last_access=1,
        output=types.SimpleNamespace(view=None), window=win, quick_mode=False,
    )


class TestCloseRefreshesTheList(unittest.TestCase):
    """Closing a session sheet repaints the Sessions list itself.

    Regression: the close commands stopped the session and closed the sheet
    without telling the list, so a closed session sat in CURRENT until the poll
    happened to notice — up to the 8s backoff, never while another list's clock
    took over the next wake.
    """

    def setUp(self):
        from tests.stubs import install
        self._prev_sublime = getattr(sl, "sublime", None)
        self.sublime = install()
        sl.sublime = self.sublime
        self._pending = sl._refresh_pending
        self._armed = sl._poll_armed
        self._timeouts = []
        self.sublime.windows = lambda: []
        self.sublime.set_timeout = lambda f, t=0: self._timeouts.append(t)

    def tearDown(self):
        sl.sublime = self._prev_sublime
        sl._refresh_pending = self._pending
        sl._poll_armed = self._armed

    def _list_window(self):
        from tests.stubs import FakeView, FakeWindow
        view = FakeView(5)
        view.settings().set(sl.SETTING, True)
        win = FakeWindow()
        win._views.append(view)
        view.window = lambda: win
        self.sublime.windows = lambda: [win]
        return win, view

    def test_closing_a_sheet_schedules_a_list_refresh(self):
        from tests.stubs import FakeView
        from ui.listeners import SubmarineEventListener
        self._list_window()
        sl._refresh_pending = False
        sl._poll_armed = False
        SubmarineEventListener().on_close(FakeView(9))
        self.assertTrue(sl._refresh_pending)
        self.assertIn(50, self._timeouts)
        self.assertTrue(sl._poll_armed, "the refresh must keep the poll armed")

    def test_focusing_a_list_arms_the_poll_and_renders(self):
        """A list restored with the window never armed a poll; opening it must
        bring it current and keep its clock going."""
        sl._refresh_pending = False
        sl._poll_armed = False
        win, view = self._list_window()
        renders = []
        real = sl.SessionListView

        class _Recording(object):
            def refresh(self, follow=False):
                renders.append(self.view.id())
        sl.SessionListView = _Recording
        try:
            sl.SessionListClickListener().on_activated(view)
        finally:
            sl.SessionListView = real
        self.assertEqual(renders, [view.id()])
        self.assertTrue(sl._poll_armed)

    def test_a_close_without_a_list_open_costs_nothing(self):
        from tests.stubs import FakeView
        from ui.listeners import SubmarineEventListener
        sl._refresh_pending = False
        sl._poll_armed = False
        SubmarineEventListener().on_close(FakeView(9))
        self.assertFalse(sl._refresh_pending)
        self.assertEqual(self._timeouts, [])


class TestStarredConfirm(unittest.TestCase):
    """Closing a pinned row asks first; anything else goes without a question."""

    def setUp(self):
        from tests.stubs import install
        self._prev_sublime = getattr(sl, "sublime", None)
        self._prev_bookmarks = sl.load_bookmarks
        self.sublime = install()
        sl.sublime = self.sublime
        self.sublime._submarine_dialog = None
        self.sublime._submarine_dialogs = []

    def tearDown(self):
        sl.sublime = self._prev_sublime
        sl.load_bookmarks = self._prev_bookmarks
        self.sublime._submarine_dialog = None
        self.sublime._submarine_dialogs = []
        import sys
        sm = sys.modules.get("sublime")
        if sm is not None and getattr(sm, "_submarine_stub", False):
            sys.modules.pop("sublime", None)
            sys.modules.pop("sublime_plugin", None)

    def _row(self, **kw):
        row = {"kind": "saved", "session_id": "s1", "name": "kept around",
               "section": "HISTORY"}
        row.update(kw)
        return row

    def test_unstarred_row_is_not_questioned(self):
        self.sublime._submarine_dialog = False
        sl.load_bookmarks = lambda path=None: set()
        self.assertTrue(sl.starred_confirm(None, self._row()))
        self.assertEqual(self.sublime._submarine_dialogs, [])

    def test_starred_row_follows_the_answer(self):
        sl.load_bookmarks = lambda path=None: {"s1"}
        self.sublime._submarine_dialog = False
        self.assertFalse(sl.starred_confirm(None, self._row()))
        self.sublime._submarine_dialog = True
        self.assertTrue(sl.starred_confirm(None, self._row()))
        self.assertEqual(len(self.sublime._submarine_dialogs), 2)
        self.assertIn("Delete", self.sublime._submarine_dialogs[0])
        self.assertIn("kept around", self.sublime._submarine_dialogs[0])

    def test_current_starred_session_is_not_questioned(self):
        sl.load_bookmarks = lambda path=None: {"s1"}
        self.sublime._submarine_dialog = False
        row = self._row(kind="live", section="CURRENT", bound=True)
        self.assertTrue(sl.starred_confirm(None, row))
        self.assertEqual(self.sublime._submarine_dialogs, [])

    def test_active_starred_session_is_not_questioned(self):
        sl.load_bookmarks = lambda path=None: {"s1"}
        self.sublime._submarine_dialog = False
        orig = sl._row_is_current_session
        sl._row_is_current_session = lambda w, r: True
        try:
            row = self._row(kind="live", section="CURRENT", bound=False)
            self.assertTrue(sl.starred_confirm(None, row))
            self.assertEqual(self.sublime._submarine_dialogs, [])
        finally:
            sl._row_is_current_session = orig

    def test_other_live_starred_session_is_questioned(self):
        sl.load_bookmarks = lambda path=None: {"s1"}
        self.sublime._submarine_dialog = True
        orig = sl._row_is_current_session
        sl._row_is_current_session = lambda w, r: False
        try:
            row = self._row(kind="live", section="CURRENT", bound=False)
            self.assertTrue(sl.starred_confirm(None, row))
            self.assertIn("Close", self.sublime._submarine_dialogs[0])
        finally:
            sl._row_is_current_session = orig

    def test_headless_without_sublime_never_blocks(self):
        sl.sublime = None
        sl.load_bookmarks = lambda path=None: {"s1"}
        self.assertTrue(sl.starred_confirm(None, self._row()))

    def test_cmd_w_always_asks(self):
        self.sublime._submarine_dialog = False
        sl.load_bookmarks = lambda path=None: set()
        row = self._row(kind="live", section="CURRENT", bound=True, name="Trackpad")
        self.assertFalse(sl.close_confirm(None, row))
        self.sublime._submarine_dialog = True
        self.assertTrue(sl.close_confirm(None, row))
        self.assertEqual(len(self.sublime._submarine_dialogs), 2)
        self.assertIn("Trackpad", self.sublime._submarine_dialogs[0])
        self.assertIn("Close", self.sublime._submarine_dialogs[0])

    def test_cmd_w_history_asks_delete(self):
        self.sublime._submarine_dialog = True
        row = self._row(kind="saved", section="HISTORY", name="old one")
        self.assertTrue(sl.close_confirm(None, row))
        self.assertIn("Delete", self.sublime._submarine_dialogs[0])
        self.assertIn("old one", self.sublime._submarine_dialogs[0])


class TestSessionChains(unittest.TestCase):
    """One row per session, not one per resume.

    Every resume mints a new session id and a backend switch keeps the
    agent_id, so the store held one record per incarnation and HISTORY listed
    the same session over and over, sometimes under another backend's tag.
    """

    def _rec(self, sid, agent, **kw):
        row = {"session_id": sid, "agent_id": agent, "kind": "saved",
               "name": kw.pop("name", "GUEST"), "backend": kw.pop("backend", "grok"),
               "status": "closed", "view_id": None, "project": kw.pop("project", "/p"),
               "query_count": kw.pop("queries", 3),
               "last_activity": kw.pop("activity", 1),
               "last_access": kw.pop("access", 1)}
        row.update(kw)
        return row

    def test_a_chain_collapses_to_its_newest_incarnation(self):
        rows = [self._rec("old", "a1", activity=1),
                self._rec("mid", "a1", activity=2),
                self._rec("new", "a1", activity=3, backend="kimi")]
        out = sl.collapse_chains(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["session_id"], "new")
        self.assertEqual(out[0]["backend"], "kimi")
        self.assertEqual(sorted(out[0]["chain_ids"]), ["mid", "new", "old"])
        # Four sessions stay four rows.
        many = [self._rec("s%d" % i, "a%d" % i, activity=i) for i in range(4)]
        self.assertEqual(len(sl.collapse_chains(many)), 4)
        # A record with no agent_id belongs to no chain and is left alone.
        self.assertEqual(len(sl.collapse_chains([self._rec("x", None)])), 1)

    def test_a_pin_survives_the_collapse(self):
        rows = [self._rec("old", "a1", activity=1),
                self._rec("new", "a1", activity=2)]
        out = sl.collapse_chains(rows, starred={"old"})
        self.assertEqual(out[0]["session_id"], "new")
        self.assertTrue(sl._pinned(out[0], {"old"}), "the pin must ride along")
        self.assertNotIn("pinned", sl.collapse_chains(rows, starred=set())[0])

    def test_a_live_incarnation_hides_the_whole_chain(self):
        rows = [self._rec("old", "a1", activity=1),
                self._rec("new", "a1", activity=2)]
        self.assertEqual(sl.collapse_chains(rows, live_agents={"a1"}), [])

    def test_history_drops_the_older_incarnations_of_a_live_session(self):
        prev = sl.load_saved_sessions
        sl.load_saved_sessions = lambda: [
            {"session_id": "old", "agent_id": "a1", "name": "GUEST",
             "backend": "grok", "project": "/p", "state": "closed",
             "query_count": 43, "last_activity": 1, "last_access": 1},
            {"session_id": "live", "agent_id": "b1", "name": "RUNNING",
             "backend": "grok", "project": "/p", "state": "open",
             "query_count": 9, "last_activity": 2, "last_access": 2},
        ]
        try:
            here, _other = sl.collect_history(
                {"live"}, "/p", live_agents={"a1", "b1"})
        finally:
            sl.load_saved_sessions = prev
        self.assertEqual(here, [], "a live session is CURRENT, not HISTORY")

    def test_the_window_render_lists_a_resumed_session_once(self):
        from tests.stubs import install
        sublime = install()
        prev = (sl.sublime, sl.load_saved_sessions, sl.load_bookmarks)
        sl.sublime = sublime
        sl.load_saved_sessions = lambda: [
            {"session_id": "old", "agent_id": "a1", "name": "GUEST",
             "backend": "grok", "project": "/p", "state": "closed",
             "query_count": 43, "last_activity": 1, "last_access": 1},
            {"session_id": "mid", "agent_id": "a1", "name": "GUEST",
             "backend": "kimi", "project": "/p", "state": "closed",
             "query_count": 43, "last_activity": 2, "last_access": 2},
            {"session_id": "new", "agent_id": "a1", "name": "GUEST",
             "backend": "kimi", "project": "/p", "state": "open",
             "query_count": 43, "last_activity": 3, "last_access": 3},
            {"session_id": "solo", "agent_id": "b1", "name": "solo run",
             "backend": "grok", "project": "/p", "state": "closed",
             "query_count": 2, "last_activity": 4, "last_access": 4},
        ]
        sl.load_bookmarks = lambda project=None: set()
        try:
            text, index = sl.build_for_window(_win_for(["/p"]), cols=80)
        finally:
            (sl.sublime, sl.load_saved_sessions, sl.load_bookmarks) = prev
        self.assertEqual(sorted(r["session_id"] for r in index), ["new", "solo"])
        self.assertEqual(text.count("GUEST"), 1)
        self.assertIn("CURRENT (1)", text)
        self.assertIn("HISTORY (1)", text)
        self.assertIn("GUEST", text.split("CURRENT")[1].split("HISTORY")[0])
        self.assertIn("solo run", text.split("HISTORY")[1])

    def test_a_live_row_stands_for_its_whole_chain(self):
        """Grok mints a new id per resume: the registry knows only the current
        one, so a star left on an earlier incarnation must still show."""
        live = [{"kind": "live", "session_id": "new", "agent_id": "a1",
                 "name": "GUEST", "backend": "grok", "status": "ready",
                 "query_count": 43, "same_window": True, "view_id": 1,
                 "last_access": 9, "last_activity": 9}]
        saved = [{"session_id": "old", "agent_id": "a1"},
                 {"session_id": "new", "agent_id": "a1"}]
        sl.tag_live_chains(live, saved)
        self.assertEqual(sorted(sl.row_ids(live[0])), ["new", "old"])
        text, index = sl.render_list(live, [], [], starred={"old"}, cols=80)
        self.assertIn(sl.STAR_MARK + " GUEST", text)
        self.assertTrue(sl._pinned(index[0], {"old"}))
        # A lone session gets no chain to carry.
        solo = [{"kind": "live", "session_id": "s", "agent_id": "b1"}]
        sl.tag_live_chains(solo, saved)
        self.assertNotIn("chain_ids", solo[0])

    def test_pinning_a_collapsed_row_pins_every_incarnation(self):
        import json
        from tests.stubs import FakeView, install
        sublime = install()
        prev = (sl.sublime, sl.load_bookmarks, sl.load_bookmark_records,
                sl.save_bookmarks, sl.refresh_session_list)
        row = self._rec("new", "a1", section="HISTORY")
        row["chain_ids"] = ["old", "new"]
        row["line"] = 3
        saved = []

        class _Win:
            def folders(self):
                return ["/p"]

        view = FakeView(3)
        view.settings().set(sl.SETTING, True)
        view.settings().set(sl.ROWS_KEY, json.dumps([row]))
        view._sel = [types.SimpleNamespace(begin=lambda: 0)]
        view.sel = lambda: view._sel
        view.rowcol = lambda pt: (2, 0)
        win = _Win()
        view.window = lambda: win
        sl.sublime = sublime
        sl.load_bookmarks = lambda project=None: set()
        sl.load_bookmark_records = lambda project=None: {}
        sl.save_bookmarks = lambda starred, project=None, records=None: (
            saved.append(set(starred)) or True)
        sl.refresh_session_list = lambda w: None
        try:
            cmd = sl.SubmarineSessionListStarCommand()
            cmd.view = view
            cmd.run(None)
            self.assertEqual(saved[-1], {"old", "new"})
            # Pinned now: the same key unstars the whole session again.
            sl.load_bookmarks = lambda project=None: {"new"}
            cmd.run(None)
            self.assertEqual(saved[-1], set())
        finally:
            (sl.sublime, sl.load_bookmarks, sl.load_bookmark_records,
             sl.save_bookmarks, sl.refresh_session_list) = prev

    def test_starring_keeps_the_caret_on_that_session(self):
        """Pinning reorders the list; the caret must follow the starred row
        so follow-under-caret does not reveal a neighbor."""
        import json
        from tests.stubs import FakeView, install
        sublime = install()
        prev = (sl.sublime, sl.load_bookmarks, sl.load_bookmark_records,
                sl.save_bookmarks, sl.refresh_session_list)
        before = self._rec("sess", "a1", section="HISTORY")
        before["line"] = 6
        after = dict(before)
        after["line"] = 3

        class _Sel(list):
            def clear(self):
                del self[:]

            def add(self, region):
                self.append(region)

        class _R(object):
            def __init__(self, a, b=None):
                self.a = a
                self.b = a if b is None else b

            def begin(self):
                return self.a

        view = FakeView(3)
        view._content = "\n" * 12
        view.size = lambda: 12
        view.settings().set(sl.SETTING, True)
        view.settings().set(sl.ROWS_KEY, json.dumps([before]))
        view.settings().set(sl.FOLLOW_GEN_KEY, 1)
        view._sel = _Sel([_R(5)])
        view.sel = lambda: view._sel
        view.rowcol = lambda pt: (5, 0)
        view.text_point = lambda r, c: r
        view.show = lambda pt: shown.append(pt)
        view.window = lambda: types.SimpleNamespace(folders=lambda: ["/p"])
        shown = []
        sl.sublime = sublime
        sl.load_bookmarks = lambda project=None: set()
        sl.load_bookmark_records = lambda project=None: {}
        sl.save_bookmarks = lambda *a, **k: True

        def _refresh(w):
            view.settings().set(sl.ROWS_KEY, json.dumps([after]))
            view.sel().clear()
            view.sel().add(_R(99))

        sl.refresh_session_list = _refresh
        try:
            cmd = sl.SubmarineSessionListStarCommand()
            cmd.view = view
            cmd.run(None)
            caret = view.sel()[0]
            pt = caret.begin() if hasattr(caret, "begin") else caret.a
            self.assertEqual(pt, 2)
            self.assertEqual(shown, [2])
            self.assertGreater(int(view.settings().get(sl.FOLLOW_GEN_KEY)), 1)
        finally:
            (sl.sublime, sl.load_bookmarks, sl.load_bookmark_records,
             sl.save_bookmarks, sl.refresh_session_list) = prev

    def test_deleting_a_collapsed_row_drops_every_incarnation(self):
        """Delete on a starred HISTORY row used to come straight back: the
        store rows went, the bookmark and its snapshot stayed, and the next
        render rebuilt the row from the snapshot."""
        from core.records import load_bookmarks, save_bookmarks
        from tests.stubs import install
        sublime = install()
        proj = tempfile.mkdtemp(prefix="submarine-del-")
        store = [
            {"session_id": "old", "agent_id": "a1", "name": "GUEST",
             "backend": "grok", "project": proj, "state": "closed",
             "query_count": 3, "last_activity": 1, "last_access": 1},
            {"session_id": "mid", "agent_id": "a1", "name": "GUEST",
             "backend": "grok", "project": proj, "state": "closed",
             "query_count": 3, "last_activity": 2, "last_access": 2},
            {"session_id": "new", "agent_id": "a1", "name": "GUEST",
             "backend": "grok", "project": proj, "state": "closed",
             "query_count": 3, "last_activity": 3, "last_access": 3},
            {"session_id": "solo", "agent_id": "b1", "name": "solo run",
             "backend": "grok", "project": proj, "state": "closed",
             "query_count": 2, "last_activity": 4, "last_access": 4},
        ]

        def _remove(sid):
            before = len(store)
            store[:] = [r for r in store if r["session_id"] != sid]
            return len(store) != before

        prev = (sl.sublime, sl.remove_saved_session, sl.load_saved_sessions)
        sl.sublime = sublime
        sl.remove_saved_session = _remove
        sl.load_saved_sessions = lambda: [dict(r) for r in store]
        row = self._rec("new", "a1", section="HISTORY")
        row["chain_ids"] = ["old", "mid", "new"]
        try:
            win = _win_for([proj])
            # Star the whole chain, the way the star key does.
            save_bookmarks({"old", "mid", "new"}, proj, records={
                x: {"name": "GUEST", "backend": "grok", "project": proj}
                for x in ("old", "mid", "new")})
            _text, index = sl.build_for_window(win, cols=80)
            self.assertEqual(sorted(r["session_id"] for r in index),
                             ["new", "solo"])

            self.assertTrue(sl.close_row(win, row))
            self.assertEqual([r["session_id"] for r in store], ["solo"])
            self.assertEqual(load_bookmarks(proj), set())
            _text, index = sl.build_for_window(win, cols=80)
            self.assertEqual([r["session_id"] for r in index], ["solo"])

            # Counter-case: a star the store CAP pruned still lists — that
            # is what the bookmark snapshot exists for.
            save_bookmarks({"pruned"}, proj, records={
                "pruned": {"name": "kept by star", "backend": "kimi",
                           "project": proj, "query_count": 7}})
            text, index = sl.build_for_window(win, cols=80)
            self.assertEqual(sorted(r["session_id"] for r in index),
                             ["pruned", "solo"])
            self.assertIn("kept by star", text)
        finally:
            (sl.sublime, sl.remove_saved_session, sl.load_saved_sessions) = prev
            import shutil
            shutil.rmtree(proj, ignore_errors=True)


class TestRenderSessionList(unittest.TestCase):
    def tearDown(self):
        import sys
        sm = sys.modules.get("sublime")
        if sm is not None and getattr(sm, "_submarine_stub", False):
            sys.modules.pop("sublime", None)
            sys.modules.pop("sublime_plugin", None)

    def test_render_assigns_lines(self):
        live = [{
            "kind": "live", "session_id": "s1", "view_id": 1,
            "name": "Skin editor", "backend": "grok", "status": "working",
            "query_count": 3, "same_window": True,
        }]
        here = [{
            "kind": "saved", "session_id": "s2", "view_id": None,
            "name": "old plan", "backend": "kimi", "status": "closed",
            "query_count": 1, "project": "/x/pil", "last_activity": 1700000000,
        }]
        text, index = sl.render_list(live, here, [], starred={"s2"})
        self.assertNotIn("STARRED", text)
        self.assertIn("CURRENT (1)", text)
        self.assertIn("HISTORY (1)", text)
        self.assertIn("Skin editor", text)
        self.assertIn(sl.STAR_MARK + " old plan", text)
        self.assertNotIn("★", text)
        self.assertIn("r rename", text)
        self.assertNotIn("refresh", text)
        self.assertIn("s star", text)
        self.assertIn("f fork", text)
        self.assertNotIn("jsonl", text)
        self.assertNotIn("c compact", text)
        compact, cidx = sl.render_list(live, here, [], starred={"s2"}, cols=24)
        self.assertIn("GR", compact)
        self.assertIn("KM", compact)
        self.assertNotIn("working", compact.split("Skin editor")[-1][:20])
        self.assertEqual(len(cidx), 2)
        self.assertEqual(len(index), 2)
        self.assertEqual(sl.row_at_line(index, index[0]["line"])["session_id"], "s1")
        self.assertEqual(sl.row_at_line(index, index[1]["line"])["session_id"], "s2")
        self.assertEqual(sl.format_when(100, now=100), "now")
        self.assertEqual(sl.format_when(100, now=130), "<1m")
        self.assertEqual(sl.format_when(100, now=159), "<1m")
        self.assertEqual(sl.format_when(100, now=100 + 5 * 60), "5m")
        self.assertEqual(sl.format_when(100, now=100 + 3 * 3600), "3h")
        self.assertEqual(sl.format_when(100, now=100 + 2 * 86400), "2d")
        self.assertEqual(sl.format_when(100, now=100 + 21 * 86400), "3w")

    def test_access_ts_prefers_focus(self):
        self.assertEqual(sl.access_ts({"last_activity": 10, "last_access": 20}), 20)
        self.assertEqual(sl.access_ts({"last_activity": 100, "last_access": 5}), 5)
        self.assertEqual(sl.access_ts({"last_activity": 10}), 10)
        self.assertEqual(sl.access_ts({}), 0)

    def test_session_title_recovers_truncated_name(self):
        conv = types.SimpleNamespace(
            prompt="check pui prop editor change, then wire the inspector")
        s = types.SimpleNamespace(
            name="check pui prop editor change,...",
            output=types.SimpleNamespace(conversations=[conv], current=None),
        )
        self.assertEqual(
            sl.session_title(s),
            "check pui prop editor change, then wire the inspector")
        s.name = "read pml_editor package"
        self.assertEqual(sl.session_title(s), "read pml_editor package")
        later = types.SimpleNamespace(
            prompt="Reply with exactly the word SWAPOK and nothing else.")
        s.name = "check pui prop editor change,..."
        s.output.conversations = [conv, later]
        self.assertEqual(
            sl.session_title(s),
            "check pui prop editor change, then wire the inspector")
        # Later turns must not steal the title; first_prompt / jsonl stem wins.
        s.name = "what solution can we use for a..."
        s.output.conversations = [types.SimpleNamespace(prompt="commit")]
        s.output.current = later
        s.first_prompt = (
            "what solution can we use for adding audio midi processing "
            "to pil? c/c++/nim")
        s._recovered_title = None
        self.assertEqual(
            sl.session_title(s),
            "what solution can we use for adding audio midi processing "
            "to pil? c/c++/nim")

    def test_saved_title_uses_first_prompt(self):
        self.assertEqual(
            sl.saved_title({
                "name": "https://github.com/ands/sprout...",
                "first_prompt": (
                    "https://github.com/ands/sproutline <- extract algorithm "
                    "from this repo"),
            }),
            "https://github.com/ands/sproutline <- extract algorithm "
            "from this repo")
        self.assertEqual(
            sl.saved_title({"name": "review", "first_prompt": "review"}),
            "review")
        self.assertEqual(
            sl.recover_title("short name", "short name but actually longer"),
            "short name")

    def test_backend_cell_aligns_deepseek(self):
        self.assertEqual(len(sl.backend_cell("grok")), sl.BACKEND_COL)
        self.assertEqual(len(sl.backend_cell("deepseek")), sl.BACKEND_COL)
        self.assertEqual(sl.backend_cell("deepseek"), "deepseek")
        live = [{
            "kind": "live", "session_id": "s1", "view_id": 1,
            "name": "fitler", "backend": "deepseek", "status": "ready",
            "query_count": 1, "same_window": True,
        }, {
            "kind": "live", "session_id": "s2", "view_id": 2,
            "name": "hello", "backend": "grok", "status": "ready",
            "query_count": 1, "same_window": True,
        }]
        text, _ = sl.render_list(live, [], [], cols=80)
        rows = [ln for ln in text.splitlines()
                if _row_mark(ln) in ("○", "●")]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0].rstrip()), len(rows[1].rstrip()))
        # No nesting, no star, no current session: no lead field at all.
        # Live rows carry the current-session column; on an unbound one it is
        # blank, and the backend cell is untouched.
        self.assertTrue(rows[0].startswith("  ○ deepseek "))
        self.assertTrue(rows[1].startswith("  ○ grok     "))
        self.assertEqual(rows[0].index("fitler"), rows[1].index("hello"))
        self.assertEqual(rows[0].index("fitler"), rows[1].index("hello"))

    def test_one_line_title_escapes_newline(self):
        self.assertEqual(
            sl.one_line_title("merge\n- **P1 — `merge` 1"),
            "merge↵- **P1 — `merge` 1")
        self.assertNotIn("\n", sl.one_line_title("a\r\nb\nc"))
        here = [{
            "kind": "saved", "session_id": "n1", "view_id": None,
            "name": "head\n- **P1 — merge", "backend": "grok",
            "status": "closed", "query_count": 0, "project": "/p",
            "last_activity": 1, "last_access": 1,
        }]
        text, _ = sl.render_list([], here, [], cols=80)
        self.assertEqual(text.count("\n- **P1"), 0)
        self.assertIn("↵", text)

    def test_same_view_compares_ids(self):
        class _V:
            def __init__(self, i):
                self._i = i

            def id(self):
                return self._i

        a, b, c = _V(1), _V(1), _V(2)
        self.assertTrue(sl._same_view(a, a))
        self.assertTrue(sl._same_view(a, b))
        self.assertFalse(sl._same_view(a, c))
        self.assertFalse(sl._same_view(a, None))

    def test_backend_abbrev(self):
        self.assertEqual(sl.backend_abbrev("grok"), "GR")
        self.assertEqual(sl.backend_abbrev("kimi"), "KM")
        self.assertEqual(sl.backend_abbrev("claude"), "CL")
        self.assertEqual(sl.backend_abbrev("codex"), "CX")

    def test_long_title_not_cut_while_space_remains(self):
        name = "=== PIL UNCAUGHT EXCEPTION === type: Library"
        live = [{
            "kind": "live", "session_id": "s1", "view_id": 1,
            "name": name, "backend": "grok", "status": "ready",
            "query_count": 1, "same_window": True,
        }]
        text, _ = sl.render_list(live, [], [], cols=80)
        row = [ln for ln in text.splitlines() if _row_mark(ln) == "○"][0]
        self.assertIn("Library", row)
        self.assertNotIn("…", row)
        self.assertIn(" 1q", row)
        self.assertTrue(row.rstrip().endswith("idle"))
        i_q = row.index("1q")
        i_st = row.rindex("idle")
        self.assertGreater(i_st - (i_q + 2), 1)

    def test_nq_closer_to_title_than_state(self):
        live = [{
            "kind": "live", "session_id": "s1", "view_id": 1,
            "name": "GUEST", "backend": "kimi", "status": "ready",
            "query_count": 10, "same_window": True,
        }]
        text, _ = sl.render_list(live, [], [], cols=80)
        row = [ln for ln in text.splitlines() if _row_mark(ln) == "○"][0]
        i_q = row.rfind("10q")
        i_st = row.rfind("idle")
        self.assertGreater(i_q, 0)
        self.assertGreater(i_st, i_q)
        self.assertEqual(row[i_q - 1], " ")
        self.assertGreater(i_st - (i_q + 3), 1)

    def test_title_uses_full_leftover(self):
        pre = "  ○ grok     "
        extra = sl._right_meta({
            "kind": "live", "status": "ready", "query_count": 1,
        })
        self.assertEqual(
            sl._name_budget(pre, extra, 80, False),
            80 - len(pre) - len(extra))
        live = [{
            "kind": "live", "session_id": "s1", "view_id": 1,
            "name": "GUEST", "backend": "kimi", "status": "ready",
            "query_count": 0, "same_window": True,
        }]
        text, _ = sl.render_list(live, [], [], cols=80)
        row = [ln for ln in text.splitlines() if _row_mark(ln) == "○"][0]
        self.assertEqual(len(row.rstrip()), 80)
        self.assertTrue(row.rstrip().endswith("idle"))

    def test_view_cols_no_double_margin(self):
        class _V:
            def viewport_extent(self):
                return (700.0, 400.0)

            def em_width(self):
                return 7.0

            def settings(self):
                return types.SimpleNamespace(get=lambda k, d=None: 6 if k == "margin" else d)

        cols = sl.view_cols(_V())
        self.assertEqual(cols, 97)

    def test_header_fits_view_cols(self):
        wide = sl.format_header(72)
        self.assertEqual(len(wide), 72)
        self.assertTrue(wide.startswith("SESSIONS"))
        self.assertTrue(wide.endswith("del close"))
        self.assertIn("enter", wide)
        self.assertIn("del", wide)
        self.assertIn("reveal", wide)
        narrow = sl.format_header(28)
        self.assertLessEqual(len(narrow), 28)
        self.assertTrue(narrow.startswith("SESSIONS"))

    def test_auto_compact_by_width(self):
        self.assertTrue(sl.use_compact(24))
        self.assertFalse(sl.use_compact(80))
        self.assertFalse(sl.use_compact(0))
        live = [{
            "kind": "live", "session_id": "s1", "view_id": 1,
            "name": "A" * 80, "backend": "grok", "status": "ready",
            "query_count": 0, "same_window": True,
        }]
        narrow, _ = sl.render_list(live, [], [], cols=24)
        nrow = [ln for ln in narrow.splitlines()
                if _row_mark(ln) == "○"][0]
        self.assertEqual(len(nrow), 24)
        self.assertIn("GR", nrow)
        self.assertNotIn("ready", nrow)
        self.assertTrue(nrow.rstrip().endswith("…"))
        wide, _ = sl.render_list(live, [], [], cols=80)
        wrow = [ln for ln in wide.splitlines()
                if _row_mark(ln) == "○"][0]
        self.assertGreater(len(wrow), len(nrow))
        self.assertIn("grok", wrow)
        self.assertIn("idle", wrow)
        self.assertEqual(len(wrow.rstrip()), 80)
        self.assertEqual(sl.fit_title("short", 10), "short     ")
        self.assertEqual(sl.fit_title("abcdefghij", 6), "abcde…")
        asleep = [{
            "kind": "live", "session_id": "z", "view_id": 2,
            "name": "nap", "backend": "kimi", "status": "sleeping",
            "query_count": 0, "same_window": True,
        }]
        sleep_txt, _ = sl.render_list(asleep, [], [], cols=80)
        srow = [ln for ln in sleep_txt.splitlines() if _row_mark(ln) == "⏸"][0]
        self.assertNotIn("sleeping", srow)

    def test_right_cols_align(self):
        live = [
            {"kind": "live", "session_id": "a", "view_id": 1,
             "name": "GUEST", "backend": "kimi", "status": "ready",
             "query_count": 0, "same_window": True,
             "last_access": 100, "last_activity": 100},
            {"kind": "live", "session_id": "b", "view_id": 2,
             "name": "inspector layout needs some polish here",
             "backend": "grok", "status": "ready",
             "query_count": 3, "same_window": True,
             "last_access": 50, "last_activity": 50},
        ]
        text, _ = sl.render_list(live, [], [], cols=80)
        rows = [ln for ln in text.splitlines()
                if _row_mark(ln) in ("○", "●", "⏸")]
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].rstrip().endswith("idle"))
        self.assertTrue(rows[1].rstrip().endswith("idle"))
        self.assertEqual(len(rows[0].rstrip()), len(rows[1].rstrip()))
        for row in rows:
            self.assertNotRegex(row, r"\b\d+[smhdw]\b")
        asleep = {
            "kind": "live", "session_id": "z", "view_id": 3,
            "name": "nap", "backend": "kimi", "status": "sleeping",
            "query_count": 0, "same_window": True,
            "last_access": 1, "last_activity": 1,
        }
        mixed, _ = sl.render_list(live + [asleep], [], [], cols=80)
        srow = [ln for ln in mixed.splitlines() if _row_mark(ln) == "⏸"][0]
        self.assertRegex(srow, r"\b(\d+[smhdw]|now)\b")
        self.assertNotIn("idle", srow)
        self.assertEqual(len(srow.rstrip()), len(rows[0].rstrip()))

    def test_history_matches_running_cols(self):
        live = [{
            "kind": "live", "session_id": "a", "view_id": 1,
            "name": "edui-shader-theme", "backend": "grok",
            "status": "sleeping", "query_count": 0, "same_window": True,
            "last_access": 1, "last_activity": 1,
        }]
        here = [{
            "kind": "saved", "session_id": "b", "view_id": None,
            "name": "add middle button panning", "backend": "grok",
            "status": "closed", "query_count": 1, "project": "/x/pil",
            "last_access": 1, "last_activity": 1,
        }]
        text, _ = sl.render_list(live, here, [], cols=80)
        run = [ln for ln in text.splitlines() if _row_mark(ln) == "⏸"][0]
        hist = [ln for ln in text.splitlines() if _row_mark(ln) == "·"][0]
        self.assertNotIn("pil", hist)
        self.assertEqual(len(run.rstrip()), len(hist.rstrip()))

    def test_live_sorts_by_access_time(self):
        from tests.stubs import install
        sublime = install()
        older = types.SimpleNamespace(
            session_id="old", name="older", backend="grok",
            working=True, is_sleeping=False, query_count=9,
            last_activity=100, last_access=100,
            output=types.SimpleNamespace(view=None), window=None,
            quick_mode=False,
        )
        newer = types.SimpleNamespace(
            session_id="new", name="newer", backend="kimi",
            working=False, is_sleeping=True, query_count=1,
            last_activity=50, last_access=200,
            output=types.SimpleNamespace(view=None), window=None,
            quick_mode=False,
        )
        sublime._claude_sessions = {1: older, 2: newer}
        try:
            rows = sl.collect_live(None)
        finally:
            sublime._claude_sessions = {}
        self.assertEqual([r["session_id"] for r in rows], ["old", "new"])

    def test_input_waiting_status_and_sort(self):
        q = types.SimpleNamespace(callback=lambda *_a, **_k: None)
        waiting = types.SimpleNamespace(
            is_sleeping=False, working=True,
            output=types.SimpleNamespace(
                pending_permission=None, pending_question=q, pending_plan=None,
            ),
        )
        busy = types.SimpleNamespace(
            is_sleeping=False, working=True,
            output=types.SimpleNamespace(
                pending_permission=None, pending_question=None, pending_plan=None,
            ),
        )
        self.assertEqual(sl._status_of(waiting), "input")
        self.assertEqual(sl._status_of(busy), "working")
        compacting = types.SimpleNamespace(
            is_sleeping=False, working=False, _compacting=True,
            output=types.SimpleNamespace(
                pending_permission=None, pending_question=None, pending_plan=None,
            ),
        )
        self.assertEqual(sl._status_of(compacting), "working")
        self.assertEqual(sl._mark("input"), "?")
        self.assertEqual(sl._mark("unread"), "!")
        unread = types.SimpleNamespace(
            is_sleeping=False, working=False, unread=True, _compacting=False,
            output=types.SimpleNamespace(
                pending_permission=None, pending_question=None, pending_plan=None,
            ),
        )
        self.assertEqual(sl._status_of(unread), "unread")
        unread_working = types.SimpleNamespace(
            is_sleeping=False, working=True, unread=True, _compacting=False,
            output=types.SimpleNamespace(
                pending_permission=None, pending_question=None, pending_plan=None,
            ),
        )
        self.assertEqual(sl._status_of(unread_working), "working")
        bg_idle = types.SimpleNamespace(
            is_sleeping=False, working=False, unread=False, _compacting=False,
            output=types.SimpleNamespace(
                pending_permission=None, pending_question=None, pending_plan=None,
                active_background_tools=lambda: [object()],
            ),
        )
        self.assertEqual(sl._status_of(bg_idle), "bg")
        self.assertEqual(sl._mark("bg"), "⚙")
        row = {
            "kind": "live", "session_id": "ask", "view_id": 1,
            "name": "needs a choice", "backend": "kimi", "status": "input",
            "query_count": 2, "same_window": True,
            "last_access": 1, "last_activity": 1,
        }
        text, _ = sl.render_list([row], [], [], cols=80)
        self.assertIn("? ", text)
        self.assertIn("wait", text)
        urow = {
            "kind": "live", "session_id": "u1", "view_id": 2,
            "name": "done in background", "backend": "grok", "status": "unread",
            "query_count": 1, "same_window": True,
            "last_access": 1, "last_activity": 1,
        }
        utext, _ = sl.render_list([urow], [], [], cols=80)
        self.assertIn("! ", utext)
        self.assertIn("new", utext)
        bound_row = dict(urow)
        bound_row["bound"] = True
        bound_row["status"] = "ready"
        btext, _ = sl.render_list([bound_row, urow], [], [], cols=80)
        lines = _session_lines(btext)
        bound_line = [ln for ln in lines if _row_lead(ln)[:1] == sl.CUR_MARK][0]
        plain_line = [ln for ln in lines if _row_lead(ln)[:1] != sl.CUR_MARK][0]
        self.assertNotIn(sl.CUR_MARK, utext)
        # One lead field width for the list, so the pointer's row lands its
        # title in the same column as the row without it.
        self.assertEqual(bound_line.index("done in background"),
                         plain_line.index("done in background"))
        from tests.stubs import install
        sublime = install()
        ask = types.SimpleNamespace(
            session_id="ask", name="ask me", backend="kimi",
            working=True, is_sleeping=False, query_count=1,
            last_activity=1, last_access=1,
            output=types.SimpleNamespace(
                view=None, pending_permission=None,
                pending_question=q, pending_plan=None,
            ),
            window=None, quick_mode=False,
        )
        work = types.SimpleNamespace(
            session_id="work", name="busy", backend="grok",
            working=True, is_sleeping=False, query_count=2,
            last_activity=9, last_access=9,
            output=types.SimpleNamespace(
                view=None, pending_permission=None,
                pending_question=None, pending_plan=None,
            ),
            window=None, quick_mode=False,
        )
        sublime._claude_sessions = {1: work, 2: ask}
        try:
            rows = sl.collect_live(None)
        finally:
            sublime._claude_sessions = {}
        self.assertEqual([r["session_id"] for r in rows], ["ask", "work"])
        self.assertEqual(rows[0]["status"], "input")

    def test_current_pointer_keeps_the_state_mark(self):
        """The `▸` current pointer has its own leftmost column; it must not
        displace the row's state icon."""
        for status, mark in (("working", "●"), ("ready", "○"),
                             ("sleeping", "⏸"), ("unread", "!"),
                             ("input", "?")):
            row = {
                "kind": "live", "session_id": "s-%s" % status, "view_id": 3,
                "name": "row for %s" % status, "backend": "claude",
                "status": status, "query_count": 1, "same_window": True,
                "bound": True, "last_access": 1, "last_activity": 1,
            }
            text, _ = sl.render_list([row], [], [], cols=80)
            line = _session_lines(text)[0]
            self.assertEqual(_row_lead(line)[:1], sl.CUR_MARK, status)
            self.assertEqual(_row_mark(line), mark,
                             "%s lost its state icon to the pointer" % status)

    def test_unbound_rows_blank_the_pointer_column(self):
        row = {
            "kind": "live", "session_id": "s1", "view_id": 4,
            "name": "not shown", "backend": "claude", "status": "working",
            "query_count": 1, "same_window": True,
            "last_access": 1, "last_activity": 1,
        }
        bound = dict(row)
        bound.update(session_id="s2", view_id=5, name="shown", bound=True)
        text, _ = sl.render_list([bound, row], [], [], cols=80)
        lines = _session_lines(text)
        bline = [ln for ln in lines if "shown" in ln][0]
        line = [ln for ln in lines if "not shown" in ln][0]
        self.assertEqual(_row_lead(bline)[:1], sl.CUR_MARK)
        self.assertNotIn(sl.CUR_MARK, _row_lead(line))
        self.assertEqual(_row_mark(line), "●")
        self.assertEqual(_row_mark(bline), "●")
        # The lead field is one width for the list, so titles stay aligned.
        self.assertEqual(line.index("not shown"), bline.index("shown"))

    def test_wide_pin_is_counted_in_the_columns(self):
        """✨ renders two cells: the row is still exactly `cols` cells wide and
        the meta after it lands where every other row's does."""
        rows_in = [
            _saved("a", "sparkled", access=2, queries=3),
            _saved("b", "plain-row", access=1, queries=4),
        ]
        text, index = sl.render_list([], rows_in, [], {"a"}, cols=60)
        lines = text.splitlines()
        starred = [ln for ln in lines if "sparkled" in ln][0]
        plain = [ln for ln in lines if "plain-row" in ln][0]
        self.assertEqual(sl.cell_width(starred), 60)
        self.assertEqual(sl.cell_width(plain), 60)
        # The pad before the meta is what keeps the right edge aligned.
        self.assertEqual(sl.cell_width(starred.split("3q")[0]),
                         sl.cell_width(plain.split("4q")[0]))
        del index

    def test_wide_title_pads_to_the_column(self):
        text, _ = sl.render_list([_live("c", "宽标题会话", access=1)],
                                 [], [], cols=50)
        line = [ln for ln in text.splitlines() if "宽标题" in ln][0]
        self.assertEqual(sl.cell_width(line), 50)

    def test_history_sorts_by_access_time(self):
        prev = sl.load_saved_sessions
        sl.load_saved_sessions = lambda: [
            {"session_id": "a", "name": "old-access", "backend": "grok",
             "project": "/p", "last_activity": 300, "last_access": 300},
            {"session_id": "b", "name": "new-access", "backend": "kimi",
             "project": "/p", "last_activity": 100, "last_access": 400},
        ]
        try:
            here, other = sl.collect_history(set(), "/p")
        finally:
            sl.load_saved_sessions = prev
        self.assertEqual([r["session_id"] for r in here], ["b", "a"])
        self.assertEqual(other, [])
        self.assertEqual(sl.history_cap(), sl.HISTORY_CAP)
        # Upstream 2e6e415 raised the resume/list cap to 400; 500 here.
        self.assertEqual(sl.HISTORY_CAP, 500)

    def test_live_filters_to_window_project(self):
        from tests.stubs import install
        sublime = install()

        class _Win:
            def __init__(self, folders):
                self._folders = folders

            def folders(self):
                return self._folders

        here_win = _Win(["/proj/here"])
        other_win = _Win(["/proj/other"])
        here_s = types.SimpleNamespace(
            session_id="here", name="this project", backend="grok",
            working=False, is_sleeping=False, query_count=1,
            last_activity=1, last_access=1,
            output=types.SimpleNamespace(view=None), window=here_win,
            quick_mode=False,
        )
        other_s = types.SimpleNamespace(
            session_id="away", name="other project", backend="kimi",
            working=True, is_sleeping=False, query_count=2,
            last_activity=2, last_access=2,
            output=types.SimpleNamespace(view=None), window=other_win,
            quick_mode=False,
        )
        sublime._claude_sessions = {1: here_s, 2: other_s}
        try:
            rows = sl.collect_live(here_win)
        finally:
            sublime._claude_sessions = {}
        self.assertEqual([r["session_id"] for r in rows], ["here"])

    def test_render_hides_other_projects(self):
        here = [{
            "kind": "saved", "session_id": "h", "view_id": None,
            "name": "here", "backend": "grok", "status": "closed",
            "query_count": 0, "project": "/p", "last_activity": 1,
        }]
        other = [{
            "kind": "saved", "session_id": "o", "view_id": None,
            "name": "elsewhere", "backend": "kimi", "status": "closed",
            "query_count": 0, "project": "/q", "last_activity": 1,
        }]
        text, index = sl.render_list([], here, other, cols=80)
        self.assertIn("here", text)
        self.assertNotIn("elsewhere", text)
        self.assertNotIn("other projects", text)
        self.assertEqual([r["session_id"] for r in index], ["h"])

    def test_starred_pins_within_current_and_history(self):
        live = [{
            "kind": "live", "session_id": "pin", "view_id": 1,
            "name": "pinned live", "backend": "grok", "status": "ready",
            "query_count": 2, "same_window": True,
            "last_access": 9, "last_activity": 9,
        }, {
            "kind": "live", "session_id": "run", "view_id": 2,
            "name": "plain live", "backend": "kimi", "status": "working",
            "query_count": 1, "same_window": True,
            "last_access": 8, "last_activity": 8,
        }]
        here = [{
            "kind": "saved", "session_id": "oldpin", "view_id": None,
            "name": "pinned hist", "backend": "grok", "status": "closed",
            "query_count": 4, "project": "/p",
            "last_access": 1, "last_activity": 1,
        }, {
            "kind": "saved", "session_id": "old", "view_id": None,
            "name": "plain hist", "backend": "kimi", "status": "closed",
            "query_count": 0, "project": "/p",
            "last_access": 2, "last_activity": 2,
        }]
        text, index = sl.render_list(live, here, [], starred={"pin", "oldpin"}, cols=80)
        self.assertNotIn("STARRED", text)
        self.assertIn("CURRENT (2)", text)
        self.assertIn("HISTORY (2)", text)
        ids = [r["session_id"] for r in index]
        self.assertEqual(ids, ["pin", "run", "oldpin", "old"])
        rows = {r["session_id"]: ln for r, ln in
                zip(index, _session_lines(text))}
        self.assertIn(sl.STAR_MARK + " pinned live", text)
        self.assertIn(sl.STAR_MARK + " pinned hist", text)
        self.assertNotIn(sl.STAR_MARK + " plain live", text)
        self.assertNotIn(sl.STAR_MARK + " plain hist", text)
        cur = text.split("CURRENT")[1].split("HISTORY")[0]
        self.assertLess(cur.find("pinned live"), cur.find("plain live"))
        hist = text.split("HISTORY")[1]
        self.assertLess(hist.find("pinned hist"), hist.find("plain hist"))

    def test_drop_empty_sessions(self):
        rows = [
            {"session_id": "a", "query_count": 0, "name": "unused"},
            {"session_id": "b", "query_count": 2, "name": "used"},
            {"session_id": "c", "query_count": 0, "name": "pinned empty"},
        ]
        kept = sl.drop_empty_sessions(rows, starred={"c"})
        self.assertEqual([r["session_id"] for r in kept], ["b", "c"])

    def test_open_saved_sessions_stay_in_current(self):
        """Restart: registry is empty; open/sleeping rows still CURRENT."""
        prev = (sl.collect_live, sl.load_saved_sessions, sl.load_bookmarks)
        sl.collect_live = lambda w: []
        sl.load_saved_sessions = lambda: [
            {"session_id": "alive", "name": "still going", "backend": "grok",
             "project": "/p", "state": "sleeping", "query_count": 4,
             "last_activity": 9, "last_access": 9},
            {"session_id": "openone", "name": "was open", "backend": "claude",
             "project": "/p", "state": "open", "query_count": 2,
             "last_activity": 8, "last_access": 8},
            {"session_id": "done", "name": "finished", "backend": "grok",
             "project": "/p", "state": "closed", "query_count": 7,
             "last_activity": 7, "last_access": 7},
            {"session_id": "empty", "name": "never used", "backend": "grok",
             "project": "/p", "state": "sleeping", "query_count": 0,
             "last_activity": 6, "last_access": 6},
        ]
        sl.load_bookmarks = lambda project=None: set()
        try:
            text, index = sl.build_for_window(_win_for(["/p"]), cols=80)
        finally:
            (sl.collect_live, sl.load_saved_sessions, sl.load_bookmarks) = prev
        by_sec = {}
        for r in index:
            by_sec.setdefault(r["section"], []).append(r["session_id"])
        self.assertEqual(set(by_sec.get("CURRENT") or []), {"alive", "openone"})
        self.assertEqual(by_sec.get("HISTORY"), ["done"])
        self.assertIn("still going", text)
        self.assertIn("was open", text)
        self.assertIn("finished", text)
        self.assertNotIn("never used", text)

    def test_window_list_keeps_empty_live(self):
        """A brand-new sheet must list in CURRENT so you can switch away."""
        prev = (sl.collect_live, sl.load_saved_sessions, sl.load_bookmarks)
        sl.collect_live = lambda w: [
            {"kind": "live", "session_id": "empty", "view_id": 1,
             "name": "brand new", "backend": "grok", "status": "ready",
             "query_count": 0, "same_window": True,
             "last_access": 2, "last_activity": 2},
            {"kind": "live", "session_id": "used", "view_id": 2,
             "name": "did work", "backend": "grok", "status": "sleeping",
             "query_count": 3, "same_window": True,
             "last_access": 1, "last_activity": 1},
        ]
        sl.load_saved_sessions = lambda: []
        sl.load_bookmarks = lambda project=None: set()
        try:
            text, index = sl.build_for_window(_win_for(["/p"]), cols=80)
        finally:
            (sl.collect_live, sl.load_saved_sessions, sl.load_bookmarks) = prev
        self.assertEqual([r["session_id"] for r in index], ["empty", "used"])
        self.assertIn("brand new", text)
        self.assertIn("did work", text)

    def test_window_list_keeps_starred_empty_live(self):
        prev = (sl.collect_live, sl.load_saved_sessions, sl.load_bookmarks)
        sl.collect_live = lambda w: [
            {"kind": "live", "session_id": "empty", "view_id": 1,
             "name": "pinned new", "backend": "grok", "status": "ready",
             "query_count": 0, "same_window": True,
             "last_access": 1, "last_activity": 1},
        ]
        sl.load_saved_sessions = lambda: []
        sl.load_bookmarks = lambda project=None: {"empty"}
        try:
            text, index = sl.build_for_window(_win_for(["/p"]), cols=80)
        finally:
            (sl.collect_live, sl.load_saved_sessions, sl.load_bookmarks) = prev
        self.assertEqual([r["session_id"] for r in index], ["empty"])
        self.assertIn("pinned new", text)

    def test_pin_starred_empty(self):
        live = [{"session_id": "a", "kind": "live", "status": "ready"}]
        here = [{"session_id": "b", "kind": "saved", "status": "closed"}]
        self.assertEqual(sl.pin_starred(live, set()), live)
        self.assertEqual(sl.pin_starred(here, set()), here)
        rows = [{
            "kind": "live", "session_id": "a", "view_id": 1,
            "name": "x", "backend": "grok", "status": "ready",
            "query_count": 0, "same_window": True,
        }]
        text, _ = sl.render_list(rows, [], [], starred=set())
        self.assertNotIn("STARRED", text)

    def test_rename_row_saved(self):
        seen = []
        sl.rename_saved_session = lambda sid, name, s=seen: (
            s.append((sid, name)) or True)
        row = {
            "kind": "saved", "session_id": "s9", "name": "old",
            "backend": "grok",
        }
        self.assertTrue(sl.rename_row(None, row, "  new title  "))
        self.assertEqual(seen, [("s9", "new title")])
        self.assertFalse(sl.rename_row(None, row, "   "))
        self.assertFalse(sl.rename_row(None, None, "x"))

    def test_close_row_history_removes_saved(self):
        gone = []
        sl.remove_saved_session = lambda sid, g=gone: (g.append(sid) or True)
        row = {
            "kind": "saved", "session_id": "dead", "name": "old",
            "backend": "grok", "section": "HISTORY",
        }
        self.assertTrue(sl.close_row(None, row))
        self.assertEqual(gone, ["dead"])
        self.assertFalse(sl.close_row(None, None))
        self.assertFalse(sl.close_row(None, {"kind": "saved"}))
        self.assertFalse(sl.close_row(None, {
            "kind": "saved", "session_id": "keep", "section": "CURRENT",
        }))
        self.assertEqual(gone, ["dead"])

    def test_close_row_current_keeps_saved(self):
        gone = []
        prev_remove = sl.remove_saved_session
        prev_live = sl._live_session_for_row
        sl.remove_saved_session = lambda sid, g=gone: (g.append(sid) or True)

        class _Sess:
            output = None
            agent_id = None
            stopped = False

            def stop(self):
                self.stopped = True

        sess = _Sess()
        sl._live_session_for_row = lambda row, s=sess: s
        row = {
            "kind": "live", "session_id": "live1", "name": "x",
            "backend": "grok", "section": "CURRENT",
        }
        try:
            self.assertTrue(sl.close_row(None, row))
            self.assertTrue(sess.stopped)
            self.assertEqual(gone, [])
            starred_live = dict(row)
            starred_live["section"] = "CURRENT"
            sess.stopped = False
            self.assertTrue(sl.close_row(None, starred_live))
            self.assertTrue(sess.stopped)
            self.assertEqual(gone, [])
        finally:
            sl.remove_saved_session = prev_remove
            sl._live_session_for_row = prev_live

    def test_render_stamps_section(self):
        live = [{
            "kind": "live", "session_id": "cur", "view_id": 1,
            "name": "now", "backend": "grok", "status": "ready",
            "query_count": 1, "same_window": True,
        }]
        here = [{
            "kind": "saved", "session_id": "old", "name": "then",
            "backend": "grok", "query_count": 3, "project": "/p",
            "last_activity": 1.0,
        }]
        _, index = sl.render_list(live, here, [], starred=set())
        by_id = {r["session_id"]: r.get("section") for r in index}
        self.assertEqual(by_id["cur"], "CURRENT")
        self.assertEqual(by_id["old"], "HISTORY")

    def test_dclick_does_not_open_neighbor(self):
        opened = []
        sl._last_open = (0.0, None)
        orig_focus = sl.focus_live
        orig_wake = sl._wake_if_sleeping
        import ui.host as host
        orig_sm = host.is_single_mode
        sl.focus_live = lambda w, r, o=opened: (o.append(r["session_id"]) or True)
        sl._wake_if_sleeping = lambda s: None
        host.is_single_mode = lambda: False
        a = {"kind": "live", "session_id": "target", "agent_id": "a1"}
        b = {"kind": "live", "session_id": "neighbor", "agent_id": "a2"}
        try:
            self.assertTrue(sl.open_row(None, a))
            # Same-key debounce still swallows a second click on `a`.
            self.assertTrue(sl.open_row(None, a))
            self.assertEqual(opened, ["target"])
        finally:
            sl.focus_live = orig_focus
            sl._wake_if_sleeping = orig_wake
            host.is_single_mode = orig_sm
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "ui", "session_list.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('view.run_command("submarine_session_list_open")', src)

    def test_tree_parent_children_grandchild_contiguous(self):
        live = [
            _live("g", "grand", aid="ag", parent="ac", access=10),
            _live("c2", "child-b", aid="ac2", parent="ap", access=20),
            _live("c1", "child-a", aid="ac", parent="ap", access=30),
            _live("p", "parent", aid="ap", access=5),
        ]
        text, index = sl.render_list(live, [], [], cols=80)
        ids = [r["session_id"] for r in index]
        self.assertEqual(ids, ["p", "c1", "g", "c2"])
        self.assertEqual([r.get("depth") for r in index], [0, 1, 2, 1])
        lines = _session_lines(text)
        by_name = {r["name"]: ln for r, ln in zip(index, lines)}
        self.assertNotIn(sl.CHILD_MARK, by_name["parent"])
        self.assertIn(sl.CHILD_MARK, by_name["child-a"])
        self.assertIn(sl.CHILD_MARK, by_name["grand"])
        self.assertIn(sl.CHILD_MARK, by_name["child-b"])
        # Flat cue, not an indent: a grandchild's title sits in the same column
        # as its parent's and its siblings'.
        self.assertLess(by_name["child-a"].index(sl.CHILD_MARK),
                        by_name["grand"].index(sl.CHILD_MARK))
        self.assertEqual(
            by_name["child-a"].index(sl.CHILD_MARK),
            by_name["child-b"].index(sl.CHILD_MARK))
        # Tree stays contiguous: grand sits between the two siblings.
        self.assertEqual(
            [ln for ln in lines if "child" in ln or "grand" in ln or "parent" in ln],
            lines)

    def test_tree_orphan_is_root(self):
        live = [
            _live("c", "orphan", aid="ac", parent="missing", access=10),
            _live("p", "other", aid="ap", access=20),
        ]
        text, index = sl.render_list(live, [], [], cols=80)
        self.assertEqual([r["session_id"] for r in index], ["p", "c"])
        self.assertEqual([r.get("depth") for r in index], [0, 0])
        self.assertNotIn(sl.CHILD_MARK, text)

    def test_tree_root_and_sibling_recency(self):
        live = [
            _live("r1", "older-root", aid="a1", access=10),
            _live("r2", "newer-root", aid="a2", access=50),
            _live("c1", "older-sib", aid="c1", parent="a2", access=20),
            _live("c2", "newer-sib", aid="c2", parent="a2", access=40),
        ]
        _, index = sl.render_list(live, [], [], cols=80)
        self.assertEqual(
            [r["session_id"] for r in index], ["r2", "c2", "c1", "r1"])
        self.assertEqual([r.get("depth") for r in index], [0, 1, 1, 0])

    def test_tree_cycle_no_hang_or_dup(self):
        live = [
            _live("a", "A", aid="aa", parent="ab", access=3),
            _live("b", "B", aid="ab", parent="aa", access=2),
            _live("c", "C", aid="ac", parent="ac", access=1),
            _live("d", "D", aid="ad", parent="ae", access=4),
            _live("e", "E", aid="ae", parent="af", access=5),
            _live("f", "F", aid="af", parent="ad", access=6),
        ]
        ordered = sl.tree_order(live)
        ids = [r["session_id"] for r in ordered]
        self.assertEqual(sorted(ids), ["a", "b", "c", "d", "e", "f"])
        self.assertEqual(len(ids), 6)
        self.assertEqual([r.get("depth") for r in ordered], [0] * 6)
        text, index = sl.render_list(live, [], [], cols=80)
        self.assertEqual(len(index), 6)
        self.assertNotIn(sl.CHILD_MARK, text)

    def test_tree_starred_child_pins_to_section_top(self):
        live = [
            _live("p", "parent", aid="ap", access=30),
            _live("c", "star-child", aid="ac", parent="ap", access=20),
            _live("g", "grand", aid="ag", parent="ac", access=15),
            _live("o", "other", aid="ao", access=10),
        ]
        text, index = sl.render_list(live, [], [], starred={"c"}, cols=80)
        self.assertEqual(
            [r["session_id"] for r in index], ["c", "g", "p", "o"])
        self.assertEqual([r.get("depth") for r in index], [0, 1, 0, 0])
        lines = _session_lines(text)
        child = [ln for ln in lines if "star-child" in ln][0]
        grand = [ln for ln in lines if "grand" in ln][0]
        parent = [ln for ln in lines if ln.endswith("parent") or " parent" in ln][0]
        self.assertIn(sl.STAR_MARK, child)
        self.assertNotIn(sl.CHILD_MARK, child)
        self.assertIn(sl.CHILD_MARK, grand)
        self.assertNotIn(sl.CHILD_MARK, parent)

    def test_tree_starred_root_carries_subtree(self):
        live = [
            _live("p", "star-root", aid="ap", access=10),
            _live("c", "kid", aid="ac", parent="ap", access=9),
            _live("g", "grand", aid="ag", parent="ac", access=8),
            _live("o", "newer-other", aid="ao", access=50),
        ]
        text, index = sl.render_list(
            live, [], [], starred={"p"}, cols=80)
        self.assertEqual(
            [r["session_id"] for r in index], ["p", "c", "g", "o"])
        self.assertEqual([r.get("depth") for r in index], [0, 1, 2, 0])
        self.assertIn(sl.STAR_MARK + " star-root", text)
        self.assertNotIn(sl.STAR_MARK + " kid", text)
        kid = [ln for ln in _session_lines(text) if "kid" in ln][0]
        self.assertIn(sl.CHILD_MARK, kid)

    def test_tree_starred_child_under_starred_root_stays_indented(self):
        live = [
            _live("p", "star-root", aid="ap", access=10),
            _live("c", "star-kid", aid="ac", parent="ap", access=9),
            _live("o", "other", aid="ao", access=50),
        ]
        text, index = sl.render_list(
            live, [], [], starred={"p", "c"}, cols=80)
        self.assertEqual([r["session_id"] for r in index], ["p", "c", "o"])
        self.assertEqual([r.get("depth") for r in index], [0, 1, 0])
        kid = [ln for ln in _session_lines(text) if "star-kid" in ln][0]
        self.assertIn(sl.CHILD_MARK, kid)
        self.assertIn(sl.STAR_MARK, kid)

    def test_tree_row_index_opens_grandchild(self):
        live = [
            _live("p", "parent", aid="ap", access=30),
            _live("c", "child", aid="ac", parent="ap", access=20),
            _live("g", "grand", aid="ag", parent="ac", access=10),
        ]
        _, index = sl.render_list(live, [], [], cols=80)
        grand = sl.row_at_line(index, index[2]["line"])
        self.assertEqual(grand["session_id"], "g")
        self.assertEqual(grand.get("depth"), 2)
        opened = []
        sl._last_open = (0.0, None)
        orig_focus = sl.focus_live
        orig_wake = sl._wake_if_sleeping
        import ui.host as host
        orig_sm = host.is_single_mode
        sl.focus_live = lambda w, r, o=opened: (o.append(r["session_id"]) or True)
        sl._wake_if_sleeping = lambda s: None
        host.is_single_mode = lambda: False
        try:
            self.assertTrue(sl.open_row(None, grand))
            self.assertEqual(opened, ["g"])
        finally:
            sl.focus_live = orig_focus
            sl._wake_if_sleeping = orig_wake
            host.is_single_mode = orig_sm

    def test_tree_column_alignment_siblings_and_deep_backend(self):
        live = [
            _live("p", "parent", aid="ap", access=10, backend="grok"),
            _live("c1", "sib-a", aid="ac1", parent="ap", access=9,
                  backend="deepseek"),
            _live("c2", "sib-b", aid="ac2", parent="ap", access=8,
                  backend="grok"),
        ]
        chain = [_live("d0", "deep-root", aid="d0", access=1, backend="grok")]
        for i in range(1, 8):
            chain.append(_live(
                "d%d" % i, "deep-%d" % i, aid="d%d" % i,
                parent="d%d" % (i - 1), access=1, backend="deepseek"))
        text, index = sl.render_list(live + chain, [], [], cols=80)
        lines = _session_lines(text)
        sib_a = [ln for ln in lines if "sib-a" in ln][0]
        sib_b = [ln for ln in lines if "sib-b" in ln][0]
        self.assertEqual(sib_a.index("deepseek"), sib_b.index("grok"))
        self.assertEqual(sib_a.index("sib-a"), sib_b.index("sib-b"))
        self.assertEqual(len(sib_a.rstrip()), 80)
        self.assertEqual(len(sib_b.rstrip()), 80)
        deep = [ln for ln in lines if "deep-7" in ln][0]
        self.assertIn(sl.CHILD_MARK, deep)
        self.assertEqual(len(deep.rstrip()), 80)
        self.assertEqual(
            sl.tree_prefix(7), sl.tree_prefix(sl.TREE_DEPTH_CAP))
        parent = [ln for ln in lines if "parent" in ln][0]
        self.assertEqual(len(parent.rstrip()), len(sib_a.rstrip()))

    def test_tree_parent_deleted_children_become_roots(self):
        kids = [
            _live("c1", "kid-a", aid="ac1", parent="ap", access=9),
            _live("c2", "kid-b", aid="ac2", parent="ap", access=8),
        ]
        with_p = [_live("p", "parent", aid="ap", access=10)] + kids
        text1, idx1 = sl.render_list(with_p, [], [], cols=80)
        self.assertEqual([r["session_id"] for r in idx1], ["p", "c1", "c2"])
        self.assertIn(sl.CHILD_MARK, text1)
        gone = []
        prev_remove = sl.remove_saved_session
        prev_live = sl._live_session_for_row

        class _Sess:
            output = None
            agent_id = "ap"
            stopped = False

            def stop(self):
                self.stopped = True

        sess = _Sess()
        sl.remove_saved_session = lambda sid, g=gone: (g.append(sid) or True)
        sl._live_session_for_row = lambda row, s=sess: s
        try:
            self.assertTrue(sl.close_row(None, idx1[0]))
            self.assertTrue(sess.stopped)
            self.assertEqual(gone, [])
        finally:
            sl.remove_saved_session = prev_remove
            sl._live_session_for_row = prev_live
        text2, idx2 = sl.render_list(kids, [], [], cols=80)
        self.assertEqual([r["session_id"] for r in idx2], ["c1", "c2"])
        self.assertEqual([r.get("depth") for r in idx2], [0, 0])
        self.assertNotIn(sl.CHILD_MARK, text2)

    def test_tree_history_uses_saved_parent_agent_id(self):
        here = [
            _saved("c", "hist-child", aid="ac", parent="ap", access=9, queries=2),
            _saved("p", "hist-parent", aid="ap", access=4, queries=3),
        ]
        text, index = sl.render_list([], here, [], cols=80)
        self.assertEqual([r["session_id"] for r in index], ["p", "c"])
        self.assertEqual([r.get("depth") for r in index], [0, 1])
        child = [ln for ln in _session_lines(text) if "hist-child" in ln][0]
        self.assertEqual(child[0], "·")   # history rows carry no current column
        self.assertIn(sl.CHILD_MARK, child)
        self.assertIn(sl.CHILD_MARK, child)

    def test_collect_live_copies_parent_agent_id(self):
        from tests.stubs import install
        sublime = install()
        parent = types.SimpleNamespace(
            session_id="p", agent_id="ap", parent_agent_id=None,
            name="parent", backend="grok", working=False, is_sleeping=False,
            query_count=1, last_activity=1, last_access=2,
            output=types.SimpleNamespace(view=None), window=None,
            quick_mode=False,
        )
        child = types.SimpleNamespace(
            session_id="c", agent_id="ac", parent_agent_id="ap",
            name="child", backend="kimi", working=False, is_sleeping=False,
            query_count=1, last_activity=1, last_access=1,
            output=types.SimpleNamespace(view=None), window=None,
            quick_mode=False,
        )
        sublime._claude_sessions = {1: parent, 2: child}
        try:
            rows = sl.collect_live(None)
        finally:
            sublime._claude_sessions = {}
        by_id = {r["session_id"]: r for r in rows}
        self.assertEqual(by_id["p"]["agent_id"], "ap")
        self.assertFalse(by_id["p"].get("parent_agent_id"))
        self.assertEqual(by_id["c"]["parent_agent_id"], "ap")

    def test_tree_compact_keeps_child_mark(self):
        live = [
            _live("p", "parent", aid="ap", access=2),
            _live("c", "child", aid="ac", parent="ap", access=1),
        ]
        text, _ = sl.render_list(live, [], [], cols=24)
        child = [ln for ln in text.splitlines() if "ch" in ln or sl.CHILD_MARK in ln]
        self.assertTrue(any(sl.CHILD_MARK in ln for ln in child))


class TestSelectActive(unittest.TestCase):
    """⌃⌘\\ opens the list with the caret on this window's active session."""

    def setUp(self):
        from tests.stubs import install_sublime
        self._sub, _ = install_sublime()
        self._prev_sublime = sl.sublime
        sl.sublime = self._sub
        self._get = sl.get_active_session

    def tearDown(self):
        sl.sublime = self._prev_sublime
        sl.get_active_session = self._get

    def _list(self, rows):
        class _Sel(list):
            def clear(s):
                del s[:]

            def add(s, region):
                s.append(region)

        class _Settings(object):
            def __init__(s, data):
                s._data = data

            def get(s, k, d=None):
                return s._data.get(k, d)

        class _View(object):
            def __init__(s):
                s._settings = _Settings({sl.ROWS_KEY: json.dumps(rows)})
                s._sel = _Sel()
                s.shown = []

            def is_valid(s):
                return True

            def settings(s):
                return s._settings

            def sel(s):
                return s._sel

            def text_point(s, row, col):
                return row * 100 + col

            def show(s, pt, show_surrounds=True):
                s.shown.append(pt)

        slv = sl.SessionListView.__new__(sl.SessionListView)
        slv.window = object()
        slv.view = _View()
        return slv

    def test_caret_lands_on_the_active_session(self):
        rows = [
            {"session_id": "a", "agent_id": "aa", "kind": "live", "line": 4},
            {"session_id": "b", "agent_id": "ab", "kind": "live", "line": 5},
        ]
        slv = self._list(rows)
        sl.get_active_session = lambda win: types.SimpleNamespace(
            agent_id="ab", session_id="b")
        self.assertTrue(slv.select_active())
        # line 5 → row 4 (0-based) → text_point 400
        self.assertEqual(slv.view.shown, [400])
        self.assertEqual(len(slv.view.sel()), 1)

    def test_no_active_session_leaves_the_caret(self):
        slv = self._list([
            {"session_id": "a", "agent_id": "aa", "kind": "live", "line": 4},
        ])
        sl.get_active_session = lambda win: None
        slv.view.sel().add("keep")
        self.assertFalse(slv.select_active())
        self.assertEqual(list(slv.view.sel()), ["keep"])
        self.assertEqual(slv.view.shown, [])

    def test_unknown_session_leaves_the_caret(self):
        slv = self._list([
            {"session_id": "a", "agent_id": "aa", "kind": "live", "line": 4},
        ])
        sl.get_active_session = lambda win: types.SimpleNamespace(
            agent_id="missing", session_id="nope")
        slv.view.sel().add("keep")
        self.assertFalse(slv.select_active())
        self.assertEqual(list(slv.view.sel()), ["keep"])

    def test_show_session_list_selects_active(self):
        from tests.stubs import FakeView, FakeWindow
        view = FakeView(5)
        view.settings().set(sl.SETTING, True)
        win = FakeWindow()
        win._views.append(view)
        selected = []
        real = sl.SessionListView

        class _SL(object):
            def _apply_chrome(self):
                pass

            def refresh(self, follow=False):
                pass

            def select_active(self):
                selected.append(self.view.id())

        sl.SessionListView = _SL
        try:
            out = sl.show_session_list(win)
            self.assertIsNotNone(out)
            self.assertEqual(selected, [5])
        finally:
            sl.SessionListView = real


class TestSessionListKeymap(unittest.TestCase):
    def setUp(self):
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        kept = []
        for line in open(os.path.join(root, "Default.sublime-keymap"),
                         encoding="utf-8"):
            if line.strip().startswith("//"):
                continue
            kept.append(re.sub(r"\s+//.*$", "", line))
        self.keymap = json.loads("\n".join(kept))

    def test_cmd_ctrl_backslash_no_longer_opens_the_list(self):
        hits = [e for e in self.keymap
                if e.get("command") == "submarine_session_list"
                and e.get("keys") == ["super+ctrl+\\"]]
        self.assertEqual(hits, [], "⌃⌘\\ is not a session-list chord")

    def test_syntax_colors_custom_provider_backends(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        syn = open(os.path.join(root, "SessionList.sublime-syntax"),
                   encoding="utf-8").read()
        self.assertIn("row_title", syn)
        self.assertIn("[A-Za-z][A-Za-z0-9_-]{0,7}", syn)
        self.assertNotIn("claude|grok|kimi|codex|pi|deepseek", syn)

    def test_cmd_w_closes_the_row_with_confirm(self):
        hits = [e for e in self.keymap
                if e.get("command") == "submarine_session_list_close"
                and e.get("keys") == ["super+w"]]
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0].get("args") or {}).get("confirm"), True)
        ctx = hits[0].get("context") or []
        self.assertTrue(any(
            c.get("key") == "setting.submarine_session_list"
            and c.get("operand") is True
            for c in ctx))


if __name__ == "__main__":
    unittest.main()
