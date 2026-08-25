"""Host-view swap operation counts: clean snapshot, dirty catch-up, fallback."""
from __future__ import annotations

import unittest

from core.registry import default_registry
from tests.test_headless_render import RecordingView
from tests.test_single_view import RecordingWindow, _session
from ui.host import HostView, set_ui_mode_override
from ui.view import SubmarineOutputView


class _SwapPerfCase(unittest.TestCase):
    def setUp(self):
        default_registry.clear()
        HostView.reset()
        set_ui_mode_override("single")

    def tearDown(self):
        default_registry.clear()
        HostView.reset()

    def _pair(self, win=None):
        win = win or RecordingWindow()
        a = _session(win, "A")
        b = _session(win, "B")
        hv = HostView.for_window(win)
        self.assertTrue(hv.attach(win, a))
        a.output.prompt("hello")
        a.output.text("first")
        a.output.meta(0.2)
        self.assertTrue(hv.attach(win, b))
        b.output.prompt("other")
        b.output.text("side")
        b.output.meta(0.1)
        # Round-trip A once more so both sessions have detach snapshots.
        self.assertTrue(hv.attach(win, a))
        self.assertTrue(hv.attach(win, b))
        return win, a, b, hv


def _spy_repaint(out):
    calls = []
    orig = out.renderer.repaint_from_state

    def spy():
        calls.append("repaint_from_state")
        return orig()

    out.renderer.repaint_from_state = spy
    out.repaint_from_state = spy
    return calls


def _spy_renderer_events(renderer, names=None):
    received = []
    names = names or (
        "prompt", "text", "tool", "tool_done", "tool_error",
        "meta", "interrupted",
    )
    origs = {}
    for name in names:
        orig = getattr(renderer, name)
        origs[name] = orig

        def _make(n, o):
            def wrapped(*a, **k):
                received.append(n)
                return o(*a, **k)
            return wrapped

        setattr(renderer, name, _make(name, orig))
    return received, origs


class TestCleanSwap(_SwapPerfCase):
    def test_clean_swap_one_replace_zero_repaint(self):
        win, a, b, hv = self._pair()
        host = b.output.view
        self.assertIsNotNone(host)
        # B is bound, A is detached unchanged.
        self.assertIsNone(a.output.view)
        snap = a.surface or a.output._surface or {}
        self.assertIsInstance(snap.get("buffer_text"), str)
        self.assertIn("hello", snap.get("buffer_text") or "")

        calls = _spy_repaint(a.output)
        host.buffer_ops = 0
        self.assertTrue(hv.attach(win, a))
        self.assertEqual(calls, [])
        self.assertLessEqual(host.buffer_ops, 2)
        self.assertGreaterEqual(host.buffer_ops, 1)
        self.assertIn("hello", host._content)
        self.assertIn("first", host._content)
        self.assertNotIn("other", host._content)


class TestDirtySwap(_SwapPerfCase):
    def test_dirty_swap_n_events_no_full_reproject(self):
        win, a, b, hv = self._pair()
        host = b.output.view
        n = 3
        a.output.text(" A")
        a.output.text(" B")
        a.output.text(" C")
        self.assertEqual(len(a.output.renderer._journal), n)

        r = a.output.renderer
        calls = _spy_repaint(a.output)
        received, _origs = _spy_renderer_events(r, names=("text", "meta", "prompt"))
        host.buffer_ops = 0
        self.assertTrue(hv.attach(win, a))
        self.assertEqual(calls, [])
        self.assertEqual(received, ["text"] * n)
        self.assertEqual(r._last_catch_up_n, n)
        self.assertIn("first", host._content)
        self.assertIn(" A", host._content)
        self.assertIn(" B", host._content)
        self.assertIn(" C", host._content)
        self.assertNotIn("other", host._content)


class TestMissingSnapshot(_SwapPerfCase):
    def test_missing_snapshot_falls_back_to_repaint(self):
        win, a, b, hv = self._pair()
        host = b.output.view
        a.output.text(" while away")
        a.surface = {}
        a.output._surface = {}
        a.output.renderer._detach_snap = None
        a.output.renderer._journal = []

        calls = _spy_repaint(a.output)
        self.assertTrue(hv.attach(win, a))
        self.assertIn("repaint_from_state", calls)
        self.assertIs(a.output.view, host)
        self.assertIn("hello", host._content)
        self.assertIn("first", host._content)
        self.assertIn("while away", host._content)


class TestDirtyChromeOnly(_SwapPerfCase):
    def test_modal_while_detached_does_not_repaint(self):
        win, a, b, hv = self._pair()
        host = b.output.view
        answers = []
        a.output.permission_request(7, "Bash", {"command": "ls"}, answers.append)
        self.assertTrue(a.output.renderer._dirty != (a.surface or {}).get("dirty"))
        self.assertEqual(a.output.renderer._journal, [])

        calls = _spy_repaint(a.output)
        self.assertTrue(hv.attach(win, a))
        self.assertEqual(calls, [])
        self.assertIn("Allow", host._content)


class TestHeadlessFakesStillWork(unittest.TestCase):
    def test_recording_view_counts_replace(self):
        win = RecordingWindow()
        out = SubmarineOutputView(win)
        v = RecordingView(view_id=1)
        v._window = win
        out.view = v
        out.restore_buffer_snapshot("hello")
        self.assertEqual(v._content, "hello")
        self.assertEqual(v.buffer_ops, 1)


if __name__ == "__main__":
    unittest.main()
