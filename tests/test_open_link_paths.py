"""Cmd+click on a path in the sheet: files open, folders open in the file
manager, and markdown's closing backtick is not part of the path."""
from __future__ import annotations

import os
import tempfile
import unittest

from tests.stubs import install_sublime


class _Region(object):
    def __init__(self, a, b=None):
        self.a = a
        self.b = a if b is None else b

    def begin(self):
        return min(self.a, self.b)

    def end(self):
        return max(self.a, self.b)


class _Window(object):
    def __init__(self, folders=()):
        self.opened = []
        self._folders = list(folders)

    def folders(self):
        return list(self._folders)

    def open_file(self, path, flags=0):
        self.opened.append(path)


class _View(object):
    def __init__(self, line):
        self.text = line
        self.win = _Window()
        self.caret = 0

    def sel(self):
        return [_Region(self.caret)]

    def line(self, pt):
        return _Region(0, len(self.text))

    def substr(self, r):
        return self.text[r.begin():r.end()]

    def window(self):
        return self.win

    def settings(self):
        return {}


class OpenLinkPathTest(unittest.TestCase):
    def setUp(self):
        install_sublime()
        import commands.text_cmds as tc
        self.tc = tc
        self.folders = []
        self._orig = tc._open_folder
        tc._open_folder = lambda p: self.folders.append(p) or True
        self.addCleanup(lambda: setattr(tc, "_open_folder", self._orig))
        self.dir = tempfile.mkdtemp(prefix="submarine-link-")
        self.file = os.path.join(self.dir, "out.txt")
        with open(self.file, "w") as f:
            f.write("x\n")

    def _click(self, line, at, folders=()):
        view = _View(line)
        view.win = _Window(folders)
        view.caret = line.index(at) + 2
        cmd = self.tc.SubmarineOpenLinkCommand.__new__(self.tc.SubmarineOpenLinkCommand)
        cmd.view = view
        cmd.run(None)
        return view

    def test_a_folder_in_backticks_opens_the_folder(self):
        self._click("results in `%s/` now" % self.dir, self.dir)
        self.assertEqual([p.rstrip("/") for p in self.folders], [self.dir])

    def test_a_file_in_backticks_still_opens(self):
        view = self._click("see `%s:1`." % self.file, self.file)
        self.assertEqual(view.win.opened, ["%s:1" % self.file])
        self.assertEqual(self.folders, [])

    def test_a_missing_path_does_nothing(self):
        view = self._click("gone `%s/nope/`" % self.dir, self.dir)
        self.assertEqual((view.win.opened, self.folders), ([], []))


    def test_a_project_relative_file_in_backticks_opens(self):
        rel = os.path.join("packages", "gfx", "shaders")
        os.makedirs(os.path.join(self.dir, rel))
        target = os.path.join(self.dir, rel, "mat.gfxp")
        with open(target, "w") as f:
            f.write("x\n")
        line = "the pass is in `packages/gfx/shaders/mat.gfxp` now"
        view = self._click(line, "shaders", folders=[self.dir])
        self.assertEqual(view.win.opened, [target])
        view = self._click("at `packages/gfx/shaders/mat.gfxp:12`", "shaders",
                           folders=[self.dir])
        self.assertEqual(view.win.opened, ["%s:12" % target])

    def test_a_project_relative_folder_in_backticks_opens(self):
        os.makedirs(os.path.join(self.dir, "packages", "gfx"))
        self._click("look in `packages/gfx/`", "gfx", folders=[self.dir])
        self.assertEqual([p.rstrip("/") for p in self.folders],
                         [os.path.join(self.dir, "packages", "gfx")])

    def test_prose_in_backticks_is_not_a_path(self):
        view = self._click("run `pil test -t all` again", "test", folders=[self.dir])
        self.assertEqual((view.win.opened, self.folders), ([], []))


if __name__ == "__main__":
    unittest.main()
