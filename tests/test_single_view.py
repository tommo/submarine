"""Host view mode: one output sheet per window, swap via HostView.attach."""
from __future__ import annotations

import inspect
import unittest

from core.registry import default_registry
from tests.fakes import make_session
from ui import keys
from ui.host import (
    PLACEHOLDER,
    HostView,
    apply_ui_mode,
    is_single_mode,
    set_ui_mode_override,
)
from ui.session_api import get_session_for_view
from ui.view import SubmarineOutputView


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


class _Sel(list):
    def clear(self):
        del self[:]

    def add(self, region):
        self.append(region)


class _Region(object):
    def __init__(self, a, b=None):
        if b is None:
            b = a
        self.a = a
        self.b = b

    def begin(self):
        return min(self.a, self.b)

    def end(self):
        return max(self.a, self.b)

    def size(self):
        return abs(self.b - self.a)


class RecordingView(object):
    def __init__(self, view_id=1):
        self._id = view_id
        self._settings = _Settings()
        self._content = ""
        self._regions = {}
        self._valid = True
        self._sel = _Sel()
        self._name = ""
        self._vp = (0.0, 0.0)
        self._window = None
        self.buffer_ops = 0
        self.closed = False

    def id(self):
        return self._id

    def is_valid(self):
        return self._valid

    def is_scratch(self):
        return True

    def file_name(self):
        return None

    def name(self):
        return self._name

    def set_name(self, name):
        self._name = name

    def size(self):
        return len(self._content)

    def substr(self, region):
        if region is None:
            return self._content
        if hasattr(region, "begin"):
            a, b = region.begin(), region.end()
        elif hasattr(region, "a"):
            a, b = region.a, region.b
        else:
            return self._content
        return self._content[a:b]

    def settings(self):
        return self._settings

    def window(self):
        return self._window

    def sel(self):
        return self._sel

    def get_regions(self, key):
        return self._regions.get(key, [])

    def add_regions(self, key, regions, *a, **k):
        self._regions[key] = list(regions)

    def erase_regions(self, key):
        self._regions.pop(key, None)

    def set_read_only(self, value):
        pass

    def is_read_only(self):
        return False

    def run_command(self, name, args=None):
        args = args or {}
        self.buffer_ops += 1
        if name in ("submarine_replace", "claude_replace"):
            start, end = args.get("start", 0), args.get("end", 0)
            text = args.get("text", "")
            self._content = self._content[:start] + text + self._content[end:]
        elif name in ("submarine_insert", "claude_insert"):
            pos = args.get("pos", len(self._content))
            text = args.get("text", "")
            self._content = self._content[:pos] + text + self._content[pos:]
        elif name == "append":
            self._content += args.get("characters", "")
        elif name in ("submarine_clear_all", "claude_clear_all"):
            self._content = ""

    def viewport_extent(self):
        return (700.0, 400.0)

    def line_height(self):
        return 16.0

    def layout_extent(self):
        return (700.0, 400.0)

    def viewport_position(self):
        return self._vp

    def set_viewport_position(self, pos, animate=False):
        self._vp = (float(pos[0]), float(pos[1]))

    def visible_region(self):
        return _Region(0, len(self._content))

    def text_to_layout(self, pt):
        return (0.0, float(pt))

    def assign_syntax(self, path):
        pass

    def set_scratch(self, v):
        pass

    def show(self, *a, **k):
        pass

    def erase_phantoms(self, name):
        pass

    def set_status(self, key, value):
        pass

    def close(self):
        self._valid = False
        self.closed = True
        win = self._window
        if win is not None:
            try:
                win._views.remove(self)
            except ValueError:
                pass


class RecordingWindow(object):
    _n = 0

    def __init__(self):
        RecordingWindow._n += 1
        self._id = RecordingWindow._n
        self._settings = _Settings()
        self._views = []
        self._folders = []
        self._active = None
        self.new_file_calls = 0

    def id(self):
        return self._id

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
        self.new_file_calls += 1
        v = RecordingView(view_id=1000 * self._id + len(self._views) + 1)
        v._window = self
        self._views.append(v)
        self._active = v
        return v

    def get_view_index(self, view):
        return (0, 0)

    def set_view_index(self, view, group, index):
        pass

    def num_groups(self):
        return 1

    def views_in_group(self, group):
        return list(self._views)


def _session(win, name="A"):
    out = SubmarineOutputView(win)
    s = make_session(
        output=out, chrome=out, window=win, registry=default_registry)
    s.name = name
    s.output.set_name(name)
    s.initialized = True
    s.session_id = "sess-%s" % name
    s.client = object()
    s.keep_running_on_close = True
    return s


class _SingleViewCase(unittest.TestCase):
    def setUp(self):
        default_registry.clear()
        HostView.reset()
        set_ui_mode_override("single")

    def tearDown(self):
        default_registry.clear()
        HostView.reset()


class TestHostSwap(_SingleViewCase):
    def test_attach_b_unbinds_a_without_stopping_bridge(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        a_client = a.client
        hv = HostView.for_window(win)
        self.assertTrue(hv.attach(win, a))
        host = hv.host_view(win)
        self.assertIs(a.output.view, host)
        self.assertIs(default_registry.for_view(host), a)
        self.assertTrue(hv.attach(win, b))
        self.assertIsNone(a.output.view)
        self.assertIs(b.output.view, host)
        self.assertIs(default_registry.for_view(host), b)
        self.assertIs(get_session_for_view(host), b)
        self.assertIn(a.agent_id, default_registry.by_agent)
        self.assertNotIn(a.agent_id, default_registry.binding.values())
        self.assertEqual(default_registry.binding.get(host.id()), b.agent_id)
        self.assertIs(a.client, a_client)
        self.assertTrue(a.initialized)
        self.assertFalse(getattr(a, "stopped", False))

    def test_attach_detach_never_stop_interrupt_sleep(self):
        src = inspect.getsource(HostView)
        for needle in ("stop(", "interrupt(", ".sleep("):
            self.assertNotIn(needle, src)
        self.assertNotIn("session.stop", src)
        self.assertNotIn("session.interrupt", src)
        self.assertNotIn("session.sleep", src)


class TestBusyKeepAlive(_SingleViewCase):
    def test_detached_turn_accumulates_and_repaints(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        host = a.output.view
        a.output.prompt("hello")
        a.output.text("first")
        a.turn.begin_query()
        self.assertTrue(a.working)
        self.assertIsNone(a.should_auto_sleep(1e12, 1))
        hv.attach(win, b)
        self.assertIsNone(a.output.view)
        self.assertTrue(a.working)
        self.assertIsNone(a.should_auto_sleep(1e12, 1))
        ops_before = host.buffer_ops
        a.output.text(" while away")
        a.output.meta(0.4)
        self.assertEqual(host.buffer_ops, ops_before)
        self.assertIn(" while away", "".join(
            e for e in a.output.current.events if isinstance(e, str)))
        hv.attach(win, a)
        self.assertIs(a.output.view, host)
        self.assertIn("hello", host._content)
        self.assertIn("first", host._content)
        self.assertIn("while away", host._content)

    def test_auto_sleep_refuses_busy(self):
        win = RecordingWindow()
        s = _session(win, "busy")
        s.turn.begin_query()
        self.assertTrue(s.working)
        self.assertIsNone(s.should_auto_sleep(1e12, 60))
        s.turn.end_live()
        s.last_idle_at = 1.0
        s.last_activity = 1.0
        self.assertIsNotNone(s.should_auto_sleep(1e12, 1))


class TestUnread(_SingleViewCase):
    def test_detached_completion_sets_unread_attach_clears(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        hv.attach(win, b)
        self.assertTrue(a._is_detached())
        a.turn_phase = "responding"
        a._set_turn_phase("idle")
        self.assertTrue(a.unread)
        hv.attach(win, a)
        self.assertFalse(a.unread)
        self.assertFalse(a._is_detached())


class TestModalDetached(_SingleViewCase):
    def test_permission_while_detached_kept_and_rerendered(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        host = a.output.view
        hv.attach(win, b)
        answers = []
        a.output.permission_request(7, "Bash", {"command": "ls"}, answers.append)
        self.assertTrue(a.output.modals.descriptors())
        self.assertEqual(a.output.modals.descriptors()[0]["kind"], "permission")
        self.assertNotIn("Allow", host._content)
        hv.attach(win, a)
        self.assertIn("Allow", host._content)
        self.assertTrue(a.output.handle_permission_key("y"))
        self.assertEqual(answers, ["allow"])
        self.assertEqual(a.output.modals.descriptors(), [])


class TestSurfaceRoundTrip(_SingleViewCase):
    def test_draft_scroll_caret_input_mode_across_two_swaps(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        host = a.output.view
        a.output.prompt("turn")
        a.output.text("ok")
        a.output.meta(0.2)
        a.output.enter_input_mode()
        a.output.set_composer_text("hello draft")
        a.output.composer._draft_caret_off = 5
        host.set_viewport_position((3.0, 41.5))
        keys.write_setting(host.settings(), keys.TASKS_EXPANDED, True)
        hv.attach(win, b)
        b.output.enter_input_mode()
        b.output.set_composer_text("other")
        hv.attach(win, a)
        self.assertTrue(a.output.is_input_mode())
        self.assertEqual(a.output.get_input_text(), "hello draft")
        self.assertEqual(a.output.composer._draft_caret_off, 5)
        self.assertEqual(host.viewport_position(), (3.0, 41.5))
        hv.attach(win, b)
        hv.attach(win, a)
        self.assertEqual(a.output.get_input_text(), "hello draft")


class TestNewSessionBindsHost(_SingleViewCase):
    def test_second_attach_does_not_new_file(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        self.assertEqual(win.new_file_calls, 1)
        host = a.output.view
        hv.attach(win, b)
        self.assertEqual(win.new_file_calls, 1)
        self.assertIs(b.output.view, host)
        b.output.show(focus=False)
        self.assertEqual(win.new_file_calls, 1)


class TestModeSwitch(_SingleViewCase):
    def test_tabs_to_single_collapses_single_to_tabs_restores(self):
        set_ui_mode_override("tabs")
        win = RecordingWindow()
        sessions = [_session(win, n) for n in ("A", "B", "C")]
        sessions[1].turn.begin_query()
        sessions[2].client = None
        sessions[2].initialized = False
        for s in sessions:
            s.output.show(focus=False, create=True)
            default_registry.register_session(s)
        self.assertEqual(win.new_file_calls, 3)
        self.assertEqual(len([v for v in win.views() if v.is_valid()]), 3)
        apply_ui_mode(win, "single")
        live_views = [v for v in win.views() if v.is_valid()]
        self.assertEqual(len(live_views), 1)
        bound = default_registry.for_view(live_views[0])
        self.assertIsNotNone(bound)
        detached = [s for s in sessions if s is not bound]
        for s in detached:
            self.assertIsNone(s.output.view)
            self.assertIn(s.agent_id, default_registry.by_agent)
        self.assertTrue(sessions[1].working)
        set_ui_mode_override("tabs")
        apply_ui_mode(win, "tabs")
        with_view = [s for s in sessions if s.output.view and s.output.view.is_valid()]
        self.assertEqual(len(with_view), 3)

    def test_tabs_to_single_closes_orphan_output_views(self):
        set_ui_mode_override("tabs")
        win = RecordingWindow()
        a = _session(win, "A")
        a.output.show(focus=False, create=True)
        default_registry.register_session(a)
        orphan = win.new_file()
        keys.write_setting(orphan.settings(), keys.OUTPUT, True)
        self.assertEqual(len([v for v in win.views() if v.is_valid()]), 2)
        apply_ui_mode(win, "single")
        live = [v for v in win.views() if v.is_valid()]
        self.assertEqual(len(live), 1)
        self.assertTrue(orphan.closed)
        self.assertIs(default_registry.for_view(live[0]), a)


class TestHostClose(_SingleViewCase):
    def test_close_backgrounds_and_reopen_reattaches(self):
        win = RecordingWindow()
        a = _session(win, "A")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        host = a.output.view
        default_registry.detach_session(a)
        hv.forget_view(host)
        host.close()
        self.assertIsNone(a.output.view)
        self.assertFalse(host.is_valid())
        self.assertIn(a.agent_id, default_registry.by_agent)
        self.assertTrue(hv.attach(win, a))
        self.assertIsNotNone(a.output.view)
        self.assertTrue(a.output.view.is_valid())
        self.assertIsNot(a.output.view, host)
        self.assertIn("A", a.output.view.name() or a.name)

    def test_detach_current_placeholder(self):
        win = RecordingWindow()
        a = _session(win, "A")
        hv = HostView.for_window(win)
        hv.attach(win, a)
        host = a.output.view
        hv.detach_current(win)
        self.assertIsNone(a.output.view)
        self.assertIn(a.agent_id, default_registry.by_agent)
        self.assertIsNone(default_registry.for_view(host))
        self.assertIn(PLACEHOLDER.strip(), host._content)


class TestAgentIdStable(_SingleViewCase):
    def test_detached_session_keeps_agent_id(self):
        win = RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        aid = a.agent_id
        hv = HostView.for_window(win)
        hv.attach(win, a)
        hv.attach(win, b)
        self.assertEqual(a.agent_id, aid)
        self.assertIs(default_registry.by_agent_id(aid), a)
        self.assertNotEqual(default_registry.binding.get(hv.host_view(win).id()), aid)


class TestDefaultIsTabs(unittest.TestCase):
    def tearDown(self):
        HostView.reset()

    def test_ui_mode_defaults_to_tabs(self):
        HostView.reset()
        self.assertFalse(is_single_mode())


if __name__ == "__main__":
    unittest.main()
