"""Closing a parent row in the Sessions list asks about its children."""
from __future__ import annotations

import json
import sys
import unittest

from ui import session_list as sl


def _row(sid, aid, parent=None, section="CURRENT", line=1, **kw):
    r = {"kind": "live", "session_id": sid, "agent_id": aid,
         "parent_agent_id": parent, "section": section, "line": line,
         "name": sid}
    r.update(kw)
    return r


class TestDescendantRows(unittest.TestCase):
    def test_collects_subtree_deepest_first_within_section(self):
        index = [
            _row("root", "a", line=1),
            _row("kid1", "b", parent="a", line=2),
            _row("grand", "c", parent="b", line=3),
            _row("kid2", "d", parent="a", line=4),
            _row("other", "e", line=5),
            # Same parent link but another section: not shown under root.
            _row("hist-kid", "f", parent="a", section="HISTORY", line=9),
        ]
        kids = sl.descendant_rows(index, index[0])
        self.assertEqual([k["session_id"] for k in kids][:1], ["grand"])
        self.assertEqual(sorted(k["session_id"] for k in kids),
                         ["grand", "kid1", "kid2"])

    def test_leaf_has_no_descendants(self):
        index = [_row("root", "a", line=1), _row("kid", "b", parent="a", line=2)]
        self.assertEqual(sl.descendant_rows(index, index[1]), [])
        self.assertEqual(sl.descendant_rows(index, {"agent_id": None}), [])

    def test_cycle_does_not_hang(self):
        index = [_row("x", "a", parent="b", line=1), _row("y", "b", parent="a", line=2)]
        self.assertEqual([k["session_id"] for k in sl.descendant_rows(index, index[0])], ["y"])


class _StubCase(unittest.TestCase):
    def setUp(self):
        from tests.stubs import install
        self._prev_sublime = getattr(sl, "sublime", None)
        self.sublime = install()
        sl.sublime = self.sublime
        self.sublime._submarine_dialog = None
        self.sublime._submarine_dialogs = []
        self.sublime._submarine_ync = self.sublime.DIALOG_CANCEL

    def tearDown(self):
        sl.sublime = self._prev_sublime
        sm = sys.modules.get("sublime")
        if sm is not None and getattr(sm, "_submarine_stub", False):
            sys.modules.pop("sublime", None)
            sys.modules.pop("sublime_plugin", None)


class TestChildrenConfirm(_StubCase):
    def test_maps_three_answers(self):
        row = _row("root", "a")
        kids = [_row("kid", "b", parent="a")]
        self.sublime._submarine_ync = self.sublime.DIALOG_YES
        self.assertIs(sl.children_confirm(None, row, kids), True)
        self.sublime._submarine_ync = self.sublime.DIALOG_NO
        self.assertIs(sl.children_confirm(None, row, kids), False)
        self.sublime._submarine_ync = self.sublime.DIALOG_CANCEL
        self.assertIsNone(sl.children_confirm(None, row, kids))
        self.assertIn("1 child session", self.sublime._submarine_dialogs[0])

    def test_no_children_no_dialog(self):
        self.assertIs(sl.children_confirm(None, _row("root", "a"), []), False)
        self.assertEqual(self.sublime._submarine_dialogs, [])


class _ListView(object):
    def __init__(self, index, line):
        self._settings = {sl.SETTING: True, sl.ROWS_KEY: json.dumps(index)}
        self._line = line
        self._win = type("W", (), {"focus_view": lambda _s, v: None})()

    def settings(self):
        d = self._settings
        return type("S", (), {"get": lambda _s, k, dflt=None: d.get(k, dflt)})()

    def sel(self):
        return [type("R", (), {"begin": lambda _s: 0})()]

    def rowcol(self, pt):
        return (self._line - 1, 0)

    def window(self):
        return self._win

    def is_valid(self):
        return True

    def text_point(self, r, c):
        return 0


class TestCloseCommandWithChildren(_StubCase):
    def setUp(self):
        super().setUp()
        self.closed = []
        self._prev = (sl.close_row, sl.refresh_session_list, sl.starred_confirm,
                      sl.close_confirm)
        sl.close_row = lambda win, row, remove=None: (self.closed.append(row["session_id"]), True)[1]
        sl.refresh_session_list = lambda win: None
        sl.starred_confirm = lambda win, row: True
        sl.close_confirm = lambda win, row: True
        self.index = [
            _row("root", "a", line=1),
            _row("kid", "b", parent="a", line=2),
            _row("grand", "c", parent="b", line=3),
            _row("other", "e", line=4),
        ]

    def tearDown(self):
        (sl.close_row, sl.refresh_session_list, sl.starred_confirm,
         sl.close_confirm) = self._prev
        super().tearDown()

    def _run(self, line, confirm=False):
        cmd = sl.SubmarineSessionListCloseCommand()
        cmd.view = _ListView(self.index, line)
        cmd.run(None, confirm=confirm)

    def test_close_all_closes_leaves_first_then_parent(self):
        self.sublime._submarine_ync = self.sublime.DIALOG_YES
        self._run(1)
        self.assertEqual(self.closed, ["grand", "kid", "root"])

    def test_only_this_closes_just_the_parent(self):
        self.sublime._submarine_ync = self.sublime.DIALOG_NO
        self._run(1)
        self.assertEqual(self.closed, ["root"])

    def test_cancel_closes_nothing(self):
        self.sublime._submarine_ync = self.sublime.DIALOG_CANCEL
        self._run(1)
        self.assertEqual(self.closed, [])

    def test_leaf_row_asks_nothing_about_children(self):
        self.sublime._submarine_ync = self.sublime.DIALOG_CANCEL  # would cancel if asked
        self._run(4)
        self.assertEqual(self.closed, ["other"])
        self.assertEqual(self.sublime._submarine_dialogs, [])

    def test_cmd_w_with_children_asks_once(self):
        asked = []
        sl.close_confirm = lambda win, row: (asked.append("close_confirm"), True)[1]
        self.sublime._submarine_ync = self.sublime.DIALOG_YES
        self._run(1, confirm=True)
        self.assertEqual(asked, [])  # children dialog replaces the plain confirm
        self.assertEqual(len(self.sublime._submarine_dialogs), 1)
        self.assertEqual(self.closed, ["grand", "kid", "root"])


if __name__ == "__main__":
    unittest.main()
