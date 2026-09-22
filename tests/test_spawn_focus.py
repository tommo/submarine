"""A tool-created session (spawn_session: a subsession, a sidecar) takes the
screen only when the user is looking at the session that asked for it.

In single-view mode every session shares one host sheet. Attaching the child
swapped that sheet out from under whatever the user was reading — another
session, or the parent while they worked in a file — even with focus=False.
"""
from __future__ import annotations

import types
import unittest

from tests.stubs import install_sublime

install_sublime()

import mcp.socket_server as ss  # noqa: E402


class _View(object):
    def __init__(self, vid, valid=True):
        self._id, self._valid = vid, valid

    def id(self):
        return self._id

    def is_valid(self):
        return self._valid


class _Window(object):
    def __init__(self, active):
        self._active = active

    def active_view(self):
        return self._active


def _parent(view):
    return types.SimpleNamespace(output=types.SimpleNamespace(view=view))


class ParentInFocusTest(unittest.TestCase):
    def test_the_parent_sheet_is_the_focused_view(self):
        sheet = _View(7)
        self.assertTrue(ss._parent_in_focus(_Window(sheet), _parent(sheet)))

    def test_focus_elsewhere_keeps_the_child_off_screen(self):
        sheet = _View(7)
        self.assertFalse(ss._parent_in_focus(_Window(_View(3)), _parent(sheet)),
                         "a file or the list has focus")

    def test_a_swapped_out_parent_never_pulls_its_child_in(self):
        self.assertFalse(ss._parent_in_focus(_Window(_View(7)), _parent(None)))

    def test_no_parent_or_no_window(self):
        self.assertFalse(ss._parent_in_focus(_Window(_View(7)), None))
        self.assertFalse(ss._parent_in_focus(None, _parent(_View(7))))


class SpawnPassesShowTest(unittest.TestCase):
    def test_spawn_asks_create_session_to_show_only_when_in_focus(self):
        import inspect
        src = inspect.getsource(ss.MCPSocketServer._spawn_session)
        self.assertIn("show = _parent_in_focus(window, parent_session)", src)
        self.assertIn("show=show", src)


if __name__ == "__main__":
    unittest.main()
