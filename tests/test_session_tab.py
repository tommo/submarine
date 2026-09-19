"""Session sheet tab position, remembered per window (group + index)."""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import plat.constants as constants  # noqa: E402
from core import placement  # noqa: E402
from core.placement import (  # noqa: E402
    apply_session_tab,
    apply_view_tab,
    is_plan_path,
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
        self._s = _Settings()
        if kind == "session":
            self._s.set("submarine_output", True)
        elif kind == "list":
            self._s.set("submarine_session_list", True)
        elif kind == "plan":
            self._s.set("submarine_plan", True)
        self._file_name = None

    def is_valid(self):
        return True

    def settings(self):
        return self._s

    def file_name(self):
        return self._file_name

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
        placement._LIVE_TABS.clear()

    def tearDown(self):
        constants.USER_PROFILES_DIR = self._orig
        placement._LIVE_TABS.clear()
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

    def test_cross_group_move_can_land_last(self):
        """From another group, the slot after every current tab is n_in: the
        clamp used to stop at n_in - 1 and drop the sheet second-to-last."""
        self._seed(1, 2)
        v = _View("sess")
        a, b = _View("a"), _View("b")
        w = _Window([[v], [a, b]], {v: (0, 0), a: (1, 0), b: (1, 1)})
        self.assertTrue(apply_session_tab(w, v))
        self.assertEqual(w.moves, [("sess", 1, 2)])
        self.assertEqual([x.name for x in w.groups[1]], ["a", "b", "sess"])

    def test_same_group_clamp_stays_within_the_tabs(self):
        # The view is one of the group's n_in tabs: last legal index is n_in - 1.
        self._seed(0, 5)
        v = _View("sess")
        a, b = _View("a"), _View("b")
        w = _Window([[v, a, b]], {v: (0, 0), a: (0, 1), b: (0, 2)})
        self.assertTrue(apply_session_tab(w, v))
        self.assertEqual(w.moves, [("sess", 0, 2)])
        self.assertEqual([x.name for x in w.groups[0]], ["a", "b", "sess"])

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


class TestTwoWindowsOneFolder(_Store):
    """Two windows opened on the same folders share one persisted row.

    While both are open each must read back its own slot; the file keeps the
    last writer for the (best-effort) restart case.
    """

    def _pair(self):
        va, vb = _View("sess-a"), _View("sess-b")
        wa = _Window([[_View("x")], [va]], {va: (1, 0)}, workspace="",
                     folders=["/p/eb"])
        wb = _Window([[vb, _View("y")], []], {vb: (0, 0)}, workspace="",
                     folders=["/p/eb"])
        wa.id = lambda: 101
        wb.id = lambda: 102
        return wa, va, wb, vb

    def test_live_windows_keep_their_own_slot(self):
        wa, va, wb, vb = self._pair()
        self.assertEqual(window_tab_key(wa), window_tab_key(wb))
        self.assertTrue(remember_session_tab(wa, va))   # group 1, index 0
        self.assertTrue(remember_session_tab(wb, vb))   # group 0, index 0
        # Neither window's own memory may be the other's.
        self.assertEqual(placement._tab_layout(wa, "session")["group"], 1)
        self.assertEqual(placement._tab_layout(wb, "session")["group"], 0)
        # A fresh sheet in window A goes back to A's split (1), not B's (0).
        fresh = _View("fresh")
        wa.groups[0].append(fresh)
        wa.layout[fresh] = (0, 1)
        self.assertTrue(apply_session_tab(wa, fresh))
        self.assertEqual(wa.moves, [("fresh", 1, 0)])

    def test_kept_apart_without_window_settings(self):
        """The in-process layer alone must tell the windows apart: a window
        whose settings cannot be read falls straight through to the shared
        file otherwise."""
        wa, va, wb, vb = self._pair()

        def _no_settings():
            raise RuntimeError("settings unavailable")

        wa.settings = _no_settings
        wb.settings = _no_settings
        self.assertTrue(remember_session_tab(wa, va))
        self.assertTrue(remember_session_tab(wb, vb))
        self.assertEqual(placement._tab_layout(wa, "session")["group"], 1)
        self.assertEqual(placement._tab_layout(wb, "session")["group"], 0)

    def test_the_file_keeps_the_last_writer(self):
        wa, va, wb, vb = self._pair()
        remember_session_tab(wa, va)
        remember_session_tab(wb, vb)
        rows = load_window_tabs()
        self.assertEqual(list(rows), ["folders:/p/eb"])
        self.assertEqual(rows["folders:/p/eb"]["session"]["group"], 0)
        # Only the in-process layer told them apart: a window that has not
        # remembered anything yet reads the shared row.
        wc = _Window([[]], {}, workspace="", folders=["/p/eb"])
        wc.id = lambda: 103
        self.assertEqual(placement._tab_layout(wc, "session")["group"], 0)


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
        self.assertEqual(view_tab_kind(_View("plan", kind="plan")), "plan")
        self.assertIsNone(view_tab_kind(_View("p", kind=None)))
        self.assertIsNone(view_tab_kind(None))

    def test_plan_md_path_is_a_plan_kind(self):
        v = _View("disk", kind=None)
        v._file_name = "/tmp/.claude/goals/g1/plan.md"
        self.assertEqual(view_tab_kind(v), "plan")
        kimi = _View("kimi", kind=None)
        kimi._file_name = (
            "/Users/x/.kimi-code/sessions/wd/sid/agents/a/plans/fancy.md")
        self.assertEqual(view_tab_kind(kimi), "plan")
        other = _View("readme", kind=None)
        other._file_name = "/tmp/README.md"
        self.assertIsNone(view_tab_kind(other))

    def test_is_plan_path(self):
        self.assertTrue(is_plan_path("/p/.claude/goals/x/plan.md"))
        self.assertTrue(is_plan_path("/p/agents/a/plans/foo.md"))
        self.assertFalse(is_plan_path("/p/README.md"))
        self.assertFalse(is_plan_path(""))
        self.assertFalse(is_plan_path(None))

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

    def test_plan_slot_is_independent_of_session_and_list(self):
        sess = _View("sess")
        listing = _View("list", kind="list")
        plan = _View("plan", kind="plan")
        w = _Window(
            [[sess], [listing], [plan]],
            {sess: (0, 0), listing: (1, 0), plan: (2, 0)},
        )
        self.assertTrue(remember_view_tab(w, sess))
        self.assertTrue(remember_view_tab(w, listing))
        self.assertTrue(remember_view_tab(w, plan))
        mem = w.settings().get("submarine_tabs")
        self.assertEqual(mem["session"]["group"], 0)
        self.assertEqual(mem["list"]["group"], 1)
        self.assertEqual(mem["plan"]["group"], 2)
        rows = json.load(open(self._path(), encoding="utf-8"))
        entry = rows["/p/eb.sublime-workspace"]
        self.assertEqual(entry["plan"]["group"], 2)

    def test_plan_slot_is_applied_on_its_own(self):
        save_window_tabs({"/p/eb.sublime-workspace": {
            "session": {"group": 0, "index": 0, "ts": 2.0},
            "plan": {"group": 1, "index": 0, "ts": 2.0},
            "ts": 2.0}})
        plan = _View("plan", kind="plan")
        w = _Window([[_View("code"), plan], []], {plan: (0, 1)})
        self.assertTrue(apply_view_tab(w, plan))
        self.assertEqual(w.moves, [("plan", 1, 0)])

    def test_open_plan_file_lands_in_the_remembered_slot(self):
        from core.placement import open_plan_file

        save_window_tabs({"/p/eb.sublime-workspace": {
            "plan": {"group": 1, "index": 0, "ts": 2.0}, "ts": 2.0}})
        w = _Window([[], []], {})

        def _open(path, flags=0):
            v = _View("opened", kind="plan")
            v._file_name = path
            w.groups[0].append(v)
            w.layout[v] = (0, 0)
            return v

        w.open_file = _open
        view = open_plan_file(w, "/tmp/x/plan.md")
        self.assertIsNotNone(view)
        self.assertTrue(view.settings().get("submarine_plan"))
        self.assertTrue(view.settings().get("word_wrap"))
        self.assertEqual(w.moves, [("opened", 1, 0)])


# ── terminal shim: a reload must end terminal sessions, not fork a second PTY ──
#
# A soft reload replaces ``Terminal._terminals`` while the old PTY's reader and
# renderer threads keep running; the next activation of the view finds no
# terminal for it and starts a second shell in the same buffer. The vendored
# package needs pyte, so the shim's hook is extracted by AST and run against a
# fake ``terminal.terminal`` module.

_SHIM = os.path.join(_ROOT, "submarine_terminal_plugin.py")


class _TermSettings(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)

    def set(self, k, v):
        self[k] = v


class _TermView:
    def __init__(self, vid, valid=True):
        self._id = vid
        self._valid = valid
        self._settings = _TermSettings({"submarine_terminal.reactivable": True})

    def id(self):
        return self._id

    def is_valid(self):
        return self._valid

    def settings(self):
        return self._settings


class _FakeTerminal:
    _terminals = {}
    _detached_terminals = []

    def __init__(self, view=None, adopted=False):
        self.view = view
        self._done = [False]
        self._adopted = adopted
        self.killed = False
        self.released = False
        if view is not None:
            _FakeTerminal._terminals[view.id()] = self
        else:
            _FakeTerminal._detached_terminals.append(self)

    def kill(self):
        self.killed = True
        if self.view is not None:
            _FakeTerminal._terminals.pop(self.view.id(), None)

    def release(self):
        self.released = True


def _shim_hook(name):
    src = open(_SHIM, encoding="utf-8").read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            ns = {}
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         _SHIM, "exec"), ns)
            return ns[name]
    return None


class TestTerminalUnload(unittest.TestCase):
    def setUp(self):
        self._saved = {k: sys.modules.get(k) for k in ("terminal", "terminal.terminal")}
        pkg = types.ModuleType("terminal")
        pkg.__path__ = []
        mod = types.ModuleType("terminal.terminal")
        mod.Terminal = _FakeTerminal
        pkg.terminal = mod
        sys.modules["terminal"] = pkg
        sys.modules["terminal.terminal"] = mod
        _FakeTerminal._terminals.clear()
        del _FakeTerminal._detached_terminals[:]

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        _FakeTerminal._terminals.clear()
        del _FakeTerminal._detached_terminals[:]

    def test_shim_defines_both_lifecycle_hooks(self):
        src = open(_SHIM, encoding="utf-8").read()
        names = {n.name for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}
        self.assertIn("plugin_loaded", names)
        self.assertIn("plugin_unloaded", names)

    def test_unload_ends_every_terminal_and_blocks_reactivation(self):
        hook = _shim_hook("plugin_unloaded")
        self.assertIsNotNone(hook)
        va, vb = _TermView(1), _TermView(2)
        ta, tb = _FakeTerminal(va), _FakeTerminal(vb)
        detached = _FakeTerminal(None)
        hook()
        for t in (ta, tb, detached):
            self.assertTrue(t.killed, t)
            self.assertTrue(t._done[0], "reader/renderer threads must be told to stop")
        for v in (va, vb):
            self.assertTrue(v.settings().get("submarine_terminal.finished"))
            self.assertFalse(v.settings().get("submarine_terminal.reactivable"))
        self.assertEqual(_FakeTerminal._terminals, {})
        self.assertEqual(_FakeTerminal._detached_terminals, [])

    def test_borrowed_pty_is_released_not_killed(self):
        hook = _shim_hook("plugin_unloaded")
        v = _TermView(3)
        t = _FakeTerminal(v, adopted=True)
        hook()
        self.assertTrue(t.released)
        self.assertFalse(t.killed)
        self.assertTrue(v.settings().get("submarine_terminal.finished"))

    def test_a_dead_view_or_failing_kill_does_not_stop_the_sweep(self):
        hook = _shim_hook("plugin_unloaded")
        gone = _TermView(4, valid=False)
        t_gone = _FakeTerminal(gone)

        class _Bad(_FakeTerminal):
            def kill(self):
                raise RuntimeError("pty already gone")

        bad = _Bad(_TermView(5))
        ok = _FakeTerminal(_TermView(6))
        hook()
        self.assertTrue(t_gone.killed)
        self.assertTrue(ok.killed)
        self.assertTrue(bad._done[0])
        self.assertEqual(_FakeTerminal._terminals, {})


if __name__ == "__main__":
    unittest.main()
