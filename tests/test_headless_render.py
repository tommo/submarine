"""Headless TurnRenderer / SubmarineOutputView: state without a buffer."""
from __future__ import annotations

import unittest

from ui.models import ToolCall
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
    """Minimal ST view that counts buffer / region / scroll ops."""

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
        self.region_ops = 0
        self.scroll_ops = 0

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
        if isinstance(region, int):
            if region < 0 or region >= len(self._content):
                return ""
            return self._content[region]
        if hasattr(region, "begin"):
            a, b = region.begin(), region.end()
        elif hasattr(region, "a"):
            a, b = region.a, region.b
        else:
            return self._content
        return self._content[a:b]

    def line(self, pt):
        text = self._content
        pt = max(0, min(int(pt), len(text)))
        a = text.rfind("\n", 0, pt) + 1
        b = text.find("\n", pt)
        if b < 0:
            b = len(text)
        return _Region(a, b)

    def settings(self):
        return self._settings

    def window(self):
        return self._window

    def sel(self):
        return self._sel

    def get_regions(self, key):
        return self._regions.get(key, [])

    def add_regions(self, key, regions, *args, **kwargs):
        self.region_ops += 1
        self._regions[key] = list(regions)

    def erase_regions(self, key):
        self.region_ops += 1
        self._regions.pop(key, None)

    def set_read_only(self, value):
        pass

    def is_read_only(self):
        return False

    def run_command(self, name, args=None):
        self.buffer_ops += 1
        args = args or {}
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

    def em_width(self):
        return 7.0

    def line_height(self):
        return 16.0

    def layout_extent(self):
        return (700.0, 400.0)

    def viewport_position(self):
        return self._vp

    def set_viewport_position(self, pos, animate=False):
        self.scroll_ops += 1
        self._vp = (float(pos[0]), float(pos[1]))

    def visible_region(self):
        return _Region(0, len(self._content))

    def text_to_layout(self, pt):
        return (0.0, float(pt))

    def layout_to_text(self, pos):
        try:
            return max(0, min(int(pos[1]), len(self._content)))
        except Exception:
            return 0

    def assign_syntax(self, path):
        pass

    def set_scratch(self, v):
        pass

    def show(self, *a, **k):
        self.scroll_ops += 1

    def erase_phantoms(self, name):
        pass

    def set_status(self, key, value):
        pass


class RecordingWindow(object):
    def __init__(self):
        self._settings = _Settings()
        self._views = []
        self._folders = []
        self._active = None
        self.new_file_calls = 0

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
        self.new_file_calls += 1
        v = RecordingView(view_id=len(self._views) + 1)
        v._window = self
        self._views.append(v)
        self._active = v
        return v


def _output(window=None):
    return SubmarineOutputView(window or RecordingWindow())


class TestHeadlessTurn(unittest.TestCase):
    def test_full_turn_records_state_without_buffer_ops(self):
        win = RecordingWindow()
        out = _output(win)
        writes = []
        orig_w, orig_r = out.sheet.write, out.sheet.replace

        def spy_w(text, pos=None):
            writes.append(("write", text, pos))
            return orig_w(text, pos)

        def spy_r(start, end, text):
            writes.append(("replace", start, end, text))
            return orig_r(start, end, text)

        out.sheet.write = spy_w
        out.sheet.replace = spy_r

        self.assertIsNone(out.view)
        out.prompt("hello world")
        out.tool("Bash", {"command": "ls"}, tool_id="t1")
        out.tool_done("Bash", "ok\n", tool_id="t1")
        out.text("all good")
        out.meta(1.5)

        self.assertEqual(win.new_file_calls, 0)
        self.assertEqual(writes, [])
        self.assertIsNotNone(out.current)
        self.assertEqual(out.current.prompt, "hello world")
        self.assertTrue(out.current.has_meta)
        self.assertFalse(out.current.working)
        names = [
            e.name for e in out.current.events if isinstance(e, ToolCall)
        ]
        self.assertIn("Bash", names)
        joined = "".join(
            e for e in out.current.events if isinstance(e, str)
        )
        self.assertIn("all good", joined)
        self.assertIsNone(out.current.region)

        # Archive the turn so conversations grows, still viewless.
        out.prompt("second")
        self.assertEqual(len(out.conversations), 1)
        self.assertEqual(out.conversations[0].prompt, "hello world")
        self.assertIsNone(out.conversations[0].region)
        self.assertEqual(writes, [])
        self.assertEqual(win.new_file_calls, 0)

    def test_bind_and_repaint_projects_full_transcript(self):
        win = RecordingWindow()
        out = _output(win)
        out.prompt("hello world")
        out.tool("Bash", {"command": "ls"}, tool_id="t1")
        out.tool_done("Bash", "ok\n", tool_id="t1")
        out.text("all good")
        out.meta(1.5)

        v = RecordingView(view_id=9)
        v._window = win
        out.view = v
        out.repaint_from_state()
        body = v._content
        self.assertIn("hello world", body)
        self.assertIn("all good", body)
        self.assertIn("Bash", body)
        self.assertIsNotNone(out.current.region)
        self.assertEqual(out.current.region[0], 0)
        self.assertGreater(out.current.region[1], 0)

    def test_detach_invalidates_regions_repaint_recomputes(self):
        win = RecordingWindow()
        out = _output(win)
        v1 = RecordingView(view_id=1)
        v1._window = win
        out.view = v1
        out.prompt("alpha")
        out.text("beta")
        out.meta(0.4)
        self.assertIsNotNone(out.current.region)
        span_before = out.current.region
        self.assertIsInstance(span_before, tuple)

        out.view = None
        self.assertIsNone(out.current.region)
        self.assertFalse(out._has_view())

        v2 = RecordingView(view_id=2)
        v2._window = win
        out.view = v2
        out.repaint_from_state()
        self.assertIsNotNone(out.current.region)
        self.assertIn("alpha", v2._content)
        self.assertIn("beta", v2._content)
        # Offsets are recomputed against v2, not carried as v1 tuples blindly
        # past a missing-region attach (repaint always rewrites from state).
        a, b = out.current.region
        self.assertEqual(a, 0)
        self.assertEqual(b, len(v2._content))

    def test_chrome_and_spinner_viewless_are_safe(self):
        out = _output()
        out.prompt("x")
        out.current.working = True
        out.advance_spinner()
        out.sleep_banner(True, "paused")
        out.queue_chips(["later"])
        out.wakeup_banner(1.0)
        out.set_status("hi")
        out.set_unread(True)
        out.clear_phantoms()
        out.enter_input_mode()
        self.assertFalse(out.is_input_mode())


if __name__ == "__main__":
    unittest.main()
