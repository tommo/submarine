"""Restarted Sublime must restore every open session asleep — no bridge, no ◎."""
from __future__ import annotations

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.registry import default_registry
from tests.fakes import FakeClient, make_session
from ui import keys


class _Settings(object):
    def __init__(self):
        self._d = {}

    def get(self, k, d=None):
        return self._d[k] if k in self._d else d

    def set(self, k, v):
        self._d[k] = v

    def erase(self, k):
        self._d.pop(k, None)

    def has(self, k):
        return k in self._d


class _View(object):
    _n = 0

    def __init__(self):
        _View._n += 1
        self._id = _View._n
        self._settings = _Settings()
        self._content = ""
        self._window = None
        self._valid = True

    def id(self):
        return self._id

    def is_valid(self):
        return self._valid

    def settings(self):
        return self._settings

    def window(self):
        return self._window

    def size(self):
        return len(self._content)

    def substr(self, region):
        return self._content

    def name(self):
        return ""

    def set_name(self, name):
        pass

    def set_read_only(self, value):
        pass

    def viewport_extent(self):
        return (700.0, 400.0)

    def em_width(self):
        return 7.0

    def erase_phantoms(self, name):
        pass

    def sel(self):
        return []

    def show(self, *a, **k):
        pass

    def run_command(self, name, args=None):
        args = args or {}
        if name in ("submarine_clear_all", "claude_clear_all"):
            self._content = ""
        elif name in ("submarine_insert", "claude_insert"):
            pos = args.get("pos", len(self._content))
            text = args.get("text", "")
            self._content = self._content[:pos] + text + self._content[pos:]


class _Window(object):
    def __init__(self):
        self._settings = _Settings()
        self._views = []
        self._folders = ["/tmp"]
        self._active = None

    def id(self):
        return 1

    def settings(self):
        return self._settings

    def folders(self):
        return list(self._folders)

    def views(self):
        return list(self._views)

    def active_view(self):
        return self._active

    def focus_view(self, v):
        self._active = v

    def new_file(self):
        v = _View()
        v._window = self
        self._views.append(v)
        self._active = v
        return v


class TestRestoreNeverStarts(unittest.TestCase):
    def test_restore_session_passes_start_false(self):
        from ui.listeners import SubmarineOutputEventListener

        src = inspect.getsource(SubmarineOutputEventListener._restore_session)
        self.assertIn("start=False", src)
        self.assertIn("show=False", src)
        self.assertIn("attach_view=view", src)

    def test_settle_startup_never_enters_composer(self):
        from ui import listeners as L

        src = inspect.getsource(L.settle_startup_output_views)
        self.assertIn("_park_startup_session_asleep", src)
        self.assertIn("allow_composer=False", src)
        self.assertNotIn("_enter_input_with_draft", src)

    def test_quiet_covers_settle(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "main.py",
        )
        with open(path, encoding="utf-8") as f:
            src = f.read()
        chunk = src.split("def plugin_loaded", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_startup_settle_views", chunk)
        self.assertIn("_end_startup_quiet", chunk)
        self.assertIn("+ 150", chunk)


class TestIsRestoringScansBackgroundSheets(unittest.TestCase):
    def test_background_reconnecting_view_counts(self):
        from tests.stubs import install_sublime

        install_sublime()
        import main

        win = _Window()
        active = win.new_file()
        bg = win.new_file()
        win._active = active
        self.assertFalse(main._is_restoring(win))
        keys.write_setting(bg.settings(), keys.RECONNECTING, True)
        self.assertTrue(main._is_restoring(win))
        self.assertTrue(main._is_restoring(None, bg))
        self.assertFalse(main._is_restoring(None, active))

    def test_create_session_does_not_start_when_orphan_is_reconnecting(self):
        from tests.stubs import install_sublime

        install_sublime()
        import main
        from core.session import Session

        default_registry.clear()
        win = _Window()
        active = win.new_file()
        orphan = win.new_file()
        win._active = active
        keys.write_setting(orphan.settings(), keys.OUTPUT, True)
        keys.write_setting(orphan.settings(), keys.RECONNECTING, True)

        started = []
        orig = Session.start

        def _start(self, *a, **k):
            started.append(True)
            return orig(self, *a, **k)

        Session.start = _start
        try:
            s = main.create_session(
                win,
                resume_id="sess-restored",
                backend="grok",
                attach_view=orphan,
            )
        finally:
            Session.start = orig
            default_registry.clear()

        self.assertEqual(started, [])
        self.assertTrue(s.is_sleeping)
        self.assertIsNone(s.client)
        self.assertFalse(s.initialized)
        self.assertFalse(s._composer_allowed)


class TestUnusedRestore(unittest.TestCase):
    def test_empty_sheet_is_unused(self):
        from ui.listeners import _matched_is_unused

        v = _View()
        self.assertTrue(_matched_is_unused(None, v))
        self.assertTrue(_matched_is_unused(
            {"session_id": "sid", "backend": "grok"}, v))

    def test_saved_turns_are_used(self):
        from ui.listeners import _matched_is_unused

        v = _View()
        self.assertFalse(_matched_is_unused(
            {"session_id": "sid", "query_count": 2}, v))
        self.assertFalse(_matched_is_unused(
            {"session_id": "sid", "first_prompt": "hello"}, v))

    def test_buffer_prompt_is_used(self):
        from ui.listeners import _matched_is_unused

        v = _View()
        v._content = "◎ hello ▶\nreply\n"
        self.assertFalse(_matched_is_unused(
            {"session_id": "sid"}, v))

    def test_idle_page_is_unused(self):
        from ui.listeners import _matched_is_unused

        v = _View()
        v._content = "◇ Submarine\nNothing is bound to this sheet.\n"
        self.assertTrue(_matched_is_unused(None, v))


class TestParkStartupAsleep(unittest.TestCase):
    def tearDown(self):
        default_registry.clear()

    def test_live_session_is_forced_asleep(self):
        from ui.listeners import _park_startup_session_asleep

        client = FakeClient()
        s = make_session(
            resume_id="sess-abc", initialized=True, client=client)
        s.session_id = "sess-abc"
        s.query_count = 2
        self.assertFalse(s.is_sleeping)
        _park_startup_session_asleep(s)
        self.assertTrue(s.is_sleeping)
        self.assertIsNone(s.client)
        self.assertFalse(s.initialized)
        self.assertFalse(s._composer_allowed)

    def test_already_sleeping_stays_sleeping(self):
        from ui.listeners import _park_startup_session_asleep

        s = make_session(resume_id="sess-abc")
        s.query_count = 2
        self.assertTrue(s.is_sleeping)
        _park_startup_session_asleep(s, paint=True)
        self.assertTrue(s.is_sleeping)
        self.assertFalse(s._composer_allowed)
        self.assertTrue(s.chrome.sleep)

    def test_unused_session_is_discarded(self):
        from ui.listeners import _park_startup_session_asleep

        win = _Window()
        view = win.new_file()
        keys.write_setting(view.settings(), keys.OUTPUT, True)
        s = make_session(resume_id="sess-empty", window=win)
        s.output.view = view
        s.query_count = 0
        default_registry.register_session(s)
        _park_startup_session_asleep(s)
        self.assertIsNone(default_registry.for_view(view))
        self.assertTrue(keys.read_setting(view.settings(), keys.IDLE))
        self.assertIn("◇ Submarine", view._content)

    def test_settle_parks_already_bound_live_session(self):
        from tests.stubs import install_sublime

        install_sublime()
        import sublime
        from ui.listeners import settle_startup_output_views
        from ui.view import SubmarineOutputView

        default_registry.clear()
        win = _Window()
        view = win.new_file()
        keys.write_setting(view.settings(), keys.OUTPUT, True)
        out = SubmarineOutputView(win)
        out.view = view
        client = FakeClient()
        s = make_session(
            output=out, chrome=out, window=win,
            resume_id="sess-live", initialized=True, client=client,
            registry=default_registry,
        )
        s.session_id = "sess-live"
        s.query_count = 3
        default_registry.register_session(s)
        self.assertFalse(s.is_sleeping)

        sublime.windows = lambda: [win]
        try:
            settle_startup_output_views()
        finally:
            sublime.windows = lambda: []
            default_registry.clear()

        self.assertTrue(s.is_sleeping)
        self.assertFalse(s._composer_allowed)
        self.assertIsNone(s.client)


if __name__ == "__main__":
    unittest.main()
