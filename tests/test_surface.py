"""surface_save/restore and modal descriptor round-trips."""
from __future__ import annotations

import unittest

from ui import keys
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

    def add_regions(self, key, regions, *args, **kwargs):
        self._regions[key] = list(regions)

    def erase_regions(self, key):
        self._regions.pop(key, None)

    def set_read_only(self, value):
        pass

    def is_read_only(self):
        return False

    def run_command(self, name, args=None):
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


class RecordingWindow(object):
    def __init__(self):
        self._settings = _Settings()
        self._views = []
        self._folders = []
        self._active = None

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
        v = RecordingView(view_id=len(self._views) + 1)
        v._window = self
        self._views.append(v)
        self._active = v
        return v


def _bind(out, view_id=1):
    win = out.window
    v = RecordingView(view_id=view_id)
    v._window = win
    out.view = v
    return v


class TestSurfaceRoundTrip(unittest.TestCase):
    def test_draft_input_scroll_caret_tasks_survive(self):
        win = RecordingWindow()
        out = SubmarineOutputView(win)
        v1 = _bind(out, 1)
        out.prompt("turn")
        out.text("ok")
        out.meta(0.2)
        out.enter_input_mode()
        self.assertTrue(out.is_input_mode())
        out.set_composer_text("hello draft")
        out.composer._draft_caret_off = 5
        v1.set_viewport_position((3.0, 41.5))
        keys.write_setting(v1.settings(), keys.TASKS_EXPANDED, True)

        snap = out.surface_save()
        self.assertEqual(snap.get("draft"), "hello draft")
        self.assertTrue(snap.get("input_mode"))
        self.assertEqual(snap.get("caret"), 5)
        self.assertEqual(snap.get("scroll"), (3.0, 41.5))
        self.assertTrue(snap.get("tasks_expanded"))

        out.view = None
        again = out.surface_save()
        self.assertEqual(again.get("draft"), "hello draft")
        self.assertTrue(again.get("input_mode"))
        self.assertEqual(again.get("caret"), 5)
        self.assertEqual(again.get("scroll"), (3.0, 41.5))
        self.assertTrue(again.get("tasks_expanded"))

        v2 = _bind(out, 2)
        out.repaint_from_state()
        out.surface_restore(snap)
        self.assertTrue(out.is_input_mode())
        self.assertEqual(out.get_input_text(), "hello draft")
        self.assertEqual(out.composer._draft_caret_off, 5)
        self.assertEqual(v2.viewport_position(), (3.0, 41.5))
        self.assertTrue(keys.read_setting(v2.settings(), keys.TASKS_EXPANDED))
        self.assertTrue(out.renderer._tasks_expanded)

    def test_restore_tolerates_missing_keys(self):
        win = RecordingWindow()
        out = SubmarineOutputView(win)
        _bind(out, 1)
        out.prompt("x")
        out.repaint_from_state()
        out.surface_restore({})
        out.surface_restore({"draft": "z"})

    def test_viewless_save_is_idempotent(self):
        out = SubmarineOutputView(RecordingWindow())
        first = out.surface_save()
        second = out.surface_save()
        self.assertEqual(first, second)
        self.assertEqual(first, {})


class TestModalDescriptor(unittest.TestCase):
    def test_permission_survives_detach_and_clears_on_answer(self):
        win = RecordingWindow()
        out = SubmarineOutputView(win)
        v1 = _bind(out, 1)
        out.prompt("need bash")
        answers = []
        out.permission_request(7, "Bash", {"command": "ls"}, answers.append)

        descs = out.modals.descriptors()
        self.assertEqual(len(descs), 1)
        self.assertEqual(descs[0]["kind"], "permission")
        self.assertEqual(descs[0]["payload"]["tool"], "Bash")
        self.assertEqual(descs[0]["payload"]["id"], 7)
        self.assertIsNotNone(out.pending_permission.region)
        self.assertIn("Allow", v1._content)

        out.view = None
        self.assertTrue(out.modals.descriptors())
        self.assertEqual(out.modals.descriptors()[0]["kind"], "permission")
        self.assertIsNone(out.pending_permission.region)
        self.assertEqual(out.pending_permission.button_regions, {})

        snap = out.surface_save()
        self.assertEqual(len(snap.get("modals") or []), 1)

        v2 = _bind(out, 2)
        out.repaint_from_state()
        self.assertNotIn("Allow", v2._content)
        out.surface_restore(snap)
        self.assertIn("Allow", v2._content)
        self.assertIsNotNone(out.pending_permission.region)
        self.assertTrue(out.modals.descriptors())

        handled = out.handle_permission_key("y")
        self.assertTrue(handled)
        self.assertEqual(answers, ["allow"])
        self.assertIsNone(out.pending_permission)
        self.assertEqual(out.modals.descriptors(), [])
        self.assertTrue(out.is_input_mode())
        self.assertIn("◎", v2._content)

    def test_plan_and_question_descriptors(self):
        win = RecordingWindow()
        out = SubmarineOutputView(win)
        _bind(out, 1)
        out.prompt("plan?")
        out.plan_approval_request(1, "/tmp/plan.md", [], lambda *_: None)
        descs = out.modals.descriptors()
        self.assertEqual(descs[0]["kind"], "plan")
        out.view = None
        self.assertEqual(out.modals.descriptors()[0]["kind"], "plan")
        self.assertIsNone(out.pending_plan.region)

        out2 = SubmarineOutputView(RecordingWindow())
        _bind(out2, 3)
        out2.prompt("q")
        out2.question_request(3, [{"question": "Pick?", "options": [{"label": "A"}]}],
                              lambda *_: None)
        self.assertEqual(out2.modals.descriptors()[0]["kind"], "question")
        out2.view = None
        self.assertEqual(out2.modals.descriptors()[0]["kind"], "question")
        self.assertIsNone(out2.pending_question.region)

    def test_approving_plan_reopens_composer(self):
        win = RecordingWindow()
        out = SubmarineOutputView(win)
        v = _bind(out, 1)
        out.prompt("plan?")
        answers = []
        out.plan_approval_request(1, "/tmp/plan.md", [], answers.append)
        self.assertFalse(out.is_input_mode())
        self.assertTrue(out.handle_plan_key("y"))
        self.assertEqual(answers, ["approve"])
        self.assertIsNone(out.pending_plan)
        self.assertTrue(out.is_input_mode())
        self.assertIn("◎", v._content)
        marker = v._content.rfind("◎")
        self.assertGreaterEqual(int(out.composer._input_start or 0), marker)

    def test_rejecting_plan_reopens_composer(self):
        win = RecordingWindow()
        out = SubmarineOutputView(win)
        v = _bind(out, 1)
        out.prompt("plan?")
        answers = []
        out.plan_approval_request(1, "/tmp/plan.md", [], answers.append)
        self.assertTrue(out.handle_plan_key("n"))
        self.assertEqual(answers, ["reject"])
        self.assertTrue(out.is_input_mode())
        self.assertIn("◎", v._content)

    def test_answering_last_question_reopens_composer(self):
        win = RecordingWindow()
        out = SubmarineOutputView(win)
        v = _bind(out, 1)
        out.prompt("ask")
        answers = []
        out.question_request(
            1, [{"question": "Pick?", "options": [{"label": "A"}]}],
            answers.append)
        self.assertTrue(out.handle_question_key("1"))
        self.assertEqual(answers, [{"Pick?": "A"}])
        self.assertIsNone(out.pending_question)
        self.assertTrue(out.is_input_mode())
        self.assertIn("◎", v._content)
        marker = v._content.rfind("◎")
        self.assertGreaterEqual(int(out.composer._input_start or 0), marker)


if __name__ == "__main__":
    unittest.main()
