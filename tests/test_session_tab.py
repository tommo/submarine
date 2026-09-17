"""Session sheet tab position, remembered per window (group + index)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import plat.constants as constants  # noqa: E402
from core.placement import (  # noqa: E402
    apply_session_tab,
    apply_view_tab,
    remember_view_tab,
    view_tab_kind,
    load_window_tabs,
    remember_session_tab,
    save_window_tabs,
    window_tab_key,
)


class _Settings(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)

    def set(self, key, value):
        self[key] = value


_UNSET = object()


class _View(object):
    def __init__(self, name, session=True, kind=_UNSET):
        # `kind=None` explicitly means "untracked"; omitted means infer.
        if kind is _UNSET:
            kind = "session" if session else None
        self.kind = kind
        self.name = name
        if kind == "session":
            self._s = {"submarine_output": True}
        elif kind == "list":
            self._s = {"submarine_session_list": True}
        else:
            self._s = {}

    def is_valid(self):
        return True

    def settings(self):
        return self._s

    def id(self):
        return id(self)

    def __repr__(self):
        return "<V %s>" % self.name


class _Window(object):
    """Minimal window: groups are lists of views, layout is view → (g, i)."""

    def __init__(self, groups, layout, workspace="/p/eb.sublime-workspace",
                 project=None, folders=None):
        self.groups = groups
        self.layout = dict(layout)
        self._s = _Settings()
        self._workspace = workspace
        self._project = project
        self._folders = folders if folders is not None else ["/p/eb"]
        self.moves = []

    # ── sublime.Window surface used by placement ──
    def settings(self):
        return self._s

    def workspace_file_name(self):
        return self._workspace

    def project_file_name(self):
        return self._project

    def folders(self):
        return self._folders

    def id(self):
        return 7

    def num_groups(self):
        return len(self.groups)

    def views_in_group(self, group):
        return list(self.groups[group])

    def active_group(self):
        return 0

    def get_view_index(self, view):
        return self.layout.get(view, (0, 0))

    def set_view_index(self, view, group, index):
        self.moves.append((view.name, group, index))
        cur_group, _cur = self.layout.get(view, (0, 0))
        if 0 <= cur_group < len(self.groups) and view in self.groups[cur_group]:
            self.groups[cur_group].remove(view)
        while len(self.groups) <= group:
            self.groups.append([])
        index = max(0, min(index, len(self.groups[group])))
        self.groups[group].insert(index, view)
        for i, v in enumerate(self.groups[group]):
            self.layout[v] = (group, i)


class _Store(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="submarine-tabs-")
        self._orig = constants.USER_PROFILES_DIR
        constants.USER_PROFILES_DIR = self._td.name

    def tearDown(self):
        constants.USER_PROFILES_DIR = self._orig
        self._td.cleanup()

    def _path(self):
        return os.path.join(self._td.name, "window_tabs.json")


class TestWindowTabKey(_Store):
    def test_prefers_the_workspace_file(self):
        w = _Window([[]], {}, workspace="/p/eb.sublime-workspace",
                    project="/p/eb.sublime-project")
        self.assertEqual(window_tab_key(w), "/p/eb.sublime-workspace")

    def test_falls_back_to_the_project_file(self):
        w = _Window([[]], {}, workspace="", project="/p/eb.sublime-project")
        self.assertEqual(window_tab_key(w), "/p/eb.sublime-project")

    def test_falls_back_to_folders(self):
        w = _Window([[]], {}, workspace=None, project=None,
                    folders=["/p/b", "/p/a"])
        self.assertEqual(window_tab_key(w), "folders:/p/a|/p/b")

    def test_falls_back_to_the_runtime_id(self):
        w = _Window([[]], {}, workspace=None, project=None, folders=[])
        self.assertEqual(window_tab_key(w), "window:7")

    def test_missing_window(self):
        self.assertEqual(window_tab_key(None), "")


class TestRemember(_Store):
    def test_records_memory_and_disk(self):
        v = _View("sess")
        w = _Window([[v], [_View("a")]], {v: (0, 0)})
        self.assertTrue(remember_session_tab(w, v))
        mem = w.settings().get("submarine_tabs")["session"]
        self.assertEqual((mem["group"], mem["index"]), (0, 0))
        rows = json.load(open(self._path(), encoding="utf-8"))
        self.assertEqual(list(rows), ["/p/eb.sublime-workspace"])
        self.assertEqual(rows["/p/eb.sublime-workspace"]["session"]["index"], 0)

    def test_records_a_moved_slot(self):
        v = _View("sess")
        other = _View("a")
        w = _Window([[], [other, v]], {other: (1, 0), v: (1, 1)})
        remember_session_tab(w, v)
        mem = w.settings().get("submarine_tabs")["session"]
        self.assertEqual((mem["group"], mem["index"]), (1, 1))

    def test_ignores_non_session_views(self):
        v = _View("plain", session=False)
        w = _Window([[v]], {v: (0, 0)})
        self.assertFalse(remember_session_tab(w, v))
        self.assertFalse(os.path.exists(self._path()))

    def test_survives_a_broken_store(self):
        v = _View("sess")
        w = _Window([[v]], {v: (0, 0)})
        with open(self._path(), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertTrue(remember_session_tab(w, v))
        self.assertIn("/p/eb.sublime-workspace",
                      json.load(open(self._path(), encoding="utf-8")))


class TestApply(_Store):
    def _seed(self, group, index):
        save_window_tabs({"/p/eb.sublime-workspace": {
            "group": group, "index": index, "ts": 1.0}})

    def test_puts_a_fresh_sheet_back_in_its_slot(self):
        self._seed(1, 0)
        v = _View("sess")
        neighbour = _View("code")
        w = _Window([[neighbour], [v]], {neighbour: (0, 0), v: (1, 0)})
        # Simulate a brand-new sheet parked at the end of group 0.
        w.layout[v] = (0, 1)
        w.groups = [[neighbour, v], []]
        self.assertTrue(apply_session_tab(w, v))
        self.assertEqual(w.moves, [("sess", 1, 0)])

    def test_uses_the_windows_own_memory_first(self):
        self._seed(1, 0)
        v = _View("sess")
        w = _Window([[v], []], {v: (0, 0)})
        w.settings().set("submarine_tabs",
                         {"session": {"group": 0, "index": 0, "ts": 2.0}})
        self.assertFalse(apply_session_tab(w, v))
        self.assertEqual(w.moves, [])

    def test_reads_a_pre_kind_window_setting(self):
        v = _View("sess")
        w = _Window([[v], []], {v: (0, 0)})
        w.settings().set("submarine_session_tab",
                         {"group": 0, "index": 0, "ts": 2.0})
        self.assertFalse(apply_session_tab(w, v))
        self.assertEqual(w.moves, [])

    def test_no_memory_is_a_noop(self):
        v = _View("sess")
        w = _Window([[v]], {v: (0, 0)})
        self.assertFalse(apply_session_tab(w, v))
        self.assertEqual(w.moves, [])

    def test_already_in_place_does_not_move(self):
        self._seed(0, 0)
        v = _View("sess")
        w = _Window([[v]], {v: (0, 0)})
        self.assertFalse(apply_session_tab(w, v))
        self.assertEqual(w.moves, [])

    def test_stale_group_falls_back_to_the_active_group(self):
        self._seed(3, 0)          # window has since lost that split
        v = _View("sess")
        w = _Window([[v], []], {v: (1, 0)})
        self.assertTrue(apply_session_tab(w, v))
        self.assertEqual(w.moves, [("sess", 0, 0)])

    def test_stale_index_is_clamped(self):
        self._seed(0, 9)          # group now holds fewer tabs
        v = _View("sess")
        other = _View("a")
        w = _Window([[v, other]], {v: (0, 0), other: (0, 1)})
        self.assertTrue(apply_session_tab(w, v))
        self.assertEqual(w.moves, [("sess", 0, 1)])

    def test_moves_into_an_empty_group(self):
        """The session sheet is usually alone in its own split."""
        self._seed(2, 0)
        v = _View("sess")
        code = _View("code")
        third = _View("third")
        w = _Window([[code, v], [], [third]],
                    {code: (0, 0), v: (0, 1), third: (2, 0)})
        self.assertTrue(apply_session_tab(w, v))
        self.assertEqual(w.moves, [("sess", 2, 0)])

    def test_corrupt_entry_is_a_noop(self):
        v = _View("sess")
        w = _Window([[v]], {v: (0, 0)})
        w.settings().set("submarine_session_tab", {"group": "nope"})
        self.assertFalse(apply_session_tab(w, v))
        self.assertEqual(w.moves, [])

    def test_ignores_non_session_views(self):
        self._seed(1, 0)
        v = _View("plain", session=False)
        w = _Window([[v], []], {v: (0, 0)})
        self.assertFalse(apply_session_tab(w, v))


class TestStore(_Store):
    def test_caps_to_the_newest_windows(self):
        tabs = {"w%d" % i: {"group": 0, "index": 0, "ts": float(i)}
                for i in range(60)}
        self.assertTrue(save_window_tabs(tabs))
        rows = load_window_tabs()
        self.assertEqual(len(rows), 40)
        self.assertIn("w59", rows)
        self.assertNotIn("w0", rows)

    def test_keeps_distinct_windows(self):
        save_window_tabs({"a": {"group": 1, "index": 2, "ts": 1.0},
                          "b": {"group": 0, "index": 5, "ts": 2.0}})
        rows = load_window_tabs()
        self.assertEqual(rows["a"]["index"], 2)
        self.assertEqual(rows["b"]["group"], 0)

    def test_drops_junk_entries(self):
        save_window_tabs({"a": {"group": 1, "index": 0, "ts": 1.0},
                          "b": {"group": 1},
                          "c": "nope",
                          "d": None})
        rows = load_window_tabs()
        self.assertEqual(list(rows), ["a"])

    def test_missing_file_is_empty(self):
        self.assertEqual(load_window_tabs(), {})

    def test_bad_input(self):
        self.assertFalse(save_window_tabs(None))
        self.assertFalse(save_window_tabs(["nope"]))


class TestViewKind(_Store):
    def test_kinds(self):
        self.assertEqual(view_tab_kind(_View("s")), "session")
        self.assertEqual(view_tab_kind(_View("l", kind="list")), "list")
        self.assertIsNone(view_tab_kind(_View("p", kind=None)))
        self.assertIsNone(view_tab_kind(None))

    def test_legacy_settings_are_recognised(self):
        old_list = _View("l", kind=None)
        old_list._s = {"claude_session_list": True}
        self.assertEqual(view_tab_kind(old_list), "list")


class TestListAndSessionSlots(_Store):
    """The list usually lives in a different split than the session sheet."""

    def test_both_kinds_are_remembered_independently(self):
        sess = _View("sess")
        listing = _View("list", kind="list")
        w = _Window([[sess], [listing]], {sess: (0, 0), listing: (1, 0)})
        self.assertTrue(remember_view_tab(w, sess))
        self.assertTrue(remember_view_tab(w, listing))
        mem = w.settings().get("submarine_tabs")
        self.assertEqual(mem["session"]["group"], 0)
        self.assertEqual(mem["list"]["group"], 1)
        rows = json.load(open(self._path(), encoding="utf-8"))
        entry = rows["/p/eb.sublime-workspace"]
        self.assertEqual(entry["session"]["group"], 0)
        self.assertEqual(entry["list"]["group"], 1)

    def test_list_slot_is_applied_on_its_own(self):
        save_window_tabs({"/p/eb.sublime-workspace": {
            "session": {"group": 3, "index": 0, "ts": 2.0},
            "list": {"group": 1, "index": 0, "ts": 2.0},
            "ts": 2.0}})
        listing = _View("list", kind="list")
        # New list sheet parked at the end of group 0.
        w = _Window([[_View("code"), listing], []], {listing: (0, 1)})
        self.assertTrue(apply_view_tab(w, listing))
        self.assertEqual(w.moves, [("list", 1, 0)])

    def test_session_kind_helpers_ignore_the_list_view(self):
        save_window_tabs({"/p/eb.sublime-workspace": {
            "list": {"group": 1, "index": 0, "ts": 2.0}, "ts": 2.0}})
        listing = _View("list", kind="list")
        w = _Window([[listing], []], {listing: (0, 0)})
        self.assertFalse(remember_session_tab(w, listing))
        self.assertFalse(apply_session_tab(w, listing))
        self.assertEqual(w.moves, [])

    def test_session_view_does_not_read_the_list_slot(self):
        save_window_tabs({"/p/eb.sublime-workspace": {
            "list": {"group": 1, "index": 0, "ts": 2.0}, "ts": 2.0}})
        sess = _View("sess")
        w = _Window([[sess], []], {sess: (0, 0)})
        self.assertFalse(apply_session_tab(w, sess))
        self.assertEqual(w.moves, [])

    def test_legacy_flat_entry_is_read_as_the_session_slot(self):
        save_window_tabs({"/p/eb.sublime-workspace": {
            "group": 2, "index": 0, "ts": 1.0}})
        sess = _View("sess")
        w = _Window([[sess], [], []], {sess: (0, 0)})
        self.assertTrue(apply_session_tab(w, sess))
        self.assertEqual(w.moves, [("sess", 2, 0)])

    def test_legacy_flat_entry_migrates_on_the_next_save(self):
        save_window_tabs({"/p/eb.sublime-workspace": {
            "group": 2, "index": 0, "ts": 1.0}})
        sess = _View("sess")
        listing = _View("list", kind="list")
        w = _Window([[], sess, [listing]], {sess: (1, 0), listing: (2, 0)})
        remember_view_tab(w, sess)
        remember_view_tab(w, listing)
        entry = json.load(open(self._path(), encoding="utf-8"))[
            "/p/eb.sublime-workspace"]
        self.assertEqual(entry["session"]["group"], 1)
        self.assertEqual(entry["list"]["group"], 2)
        self.assertNotIn("group", {k: v for k, v in entry.items()
                                   if k != "session" and k != "list"})

    def test_untracked_view_writes_nothing(self):
        plain = _View("plain", kind=None)
        w = _Window([[plain]], {plain: (0, 0)})
        self.assertFalse(remember_view_tab(w, plain))
        self.assertFalse(apply_view_tab(w, plain))
        self.assertFalse(os.path.exists(self._path()))


if __name__ == "__main__":
    unittest.main()
