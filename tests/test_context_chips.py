"""📎 chip gestures: open vs reveal, modifier-click remove, clear, line jump.

The port kept the chip *phantoms* but dropped what they do. Clicking always
opened the path (a folder or image chip opened a text editor), the selection's
line range was re-parsed by hand and the stored `action` was ignored,
modifier-click had nothing to call (`ContextManager.remove_at` had no caller
anywhere), and there was no trailing `clear` link — a mis-attached file could
only be dropped, with everything else, from the Command Palette.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.registry import SessionRegistry, default_registry
from features.context import ContextManager
from tests.fakes import make_session

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Recorder(object):
    """Stands in for the sublime module: records status + phantom bodies."""

    LAYOUT_INLINE = 2
    ENCODED_POSITION = 1

    def __init__(self):
        self.messages = []
        self.phantoms = []
        self.Region = lambda a, b=None: _Reg(a, 0 if b is None else b)
        self.Phantom = self._phantom
        self.PhantomSet = self._phantom_set

    def status_message(self, message):
        self.messages.append(message)

    def _phantom(self, region, html, layout=0, on_navigate=None):
        return {"region": region, "html": html, "on_navigate": on_navigate}

    def _phantom_set(self, view, key=None):
        return _PhantomSet(self)


class _Reg(object):
    def __init__(self, a, b):
        self.a = a
        self.b = b

    def begin(self):
        return self.a

    def end(self):
        return self.b


class _PhantomSet(object):
    def __init__(self, rec):
        self.rec = rec
        self.items = []

    def update(self, phantoms):
        self.items = list(phantoms or [])
        self.rec.phantoms = self.items


class ContextChipTest(unittest.TestCase):
    def setUp(self):
        from tests.test_headless_render import RecordingView, RecordingWindow

        self.win = RecordingWindow()
        self.view = RecordingView(view_id=71)
        self.view._window = self.win
        self.s = make_session(registry=default_registry, initialized=True)
        self.ctx = ContextManager(self.s)
        self.s.context = self.ctx
        default_registry.register(self.s)
        default_registry.bind(self.s.agent_id, self.view.id())

        from ui.view import SubmarineOutputView

        self.out = SubmarineOutputView(self.win)
        self.out.view = self.view
        self.opened = []
        self.revealed = []
        self.out._open_path = lambda path, line=None: self.opened.append(
            (path, line))
        self.out._reveal_path = lambda path: self.revealed.append(path)

        import ui.view as view_mod

        self._real_sublime = view_mod.sublime
        self.rec = _Recorder()
        view_mod.sublime = self.rec

    def tearDown(self):
        import ui.view as view_mod

        view_mod.sublime = self._real_sublime
        default_registry.clear()

    # ── pending chips (composer 📎) ─────────────────────────────────────

    def test_a_selection_chip_opens_at_its_first_line(self):
        self.ctx.add_selection("/tmp/x.py:L12-L20", "print(1)")
        self.assertEqual(self.ctx.items[0].line_range, "L12-L20")
        self.out._handle_context_href("open:0")
        self.assertEqual(self.opened, [("/tmp/x.py", 12)])
        self.assertEqual(self.revealed, [])

    def test_a_plain_file_chip_opens_with_no_line(self):
        self.ctx.add_file("/tmp/x.py", "print(1)")
        self.out._handle_context_href("open:0")
        self.assertEqual(self.opened, [("/tmp/x.py", None)])

    def test_folder_and_image_chips_reveal_instead_of_opening(self):
        self.ctx.add_folder("/tmp/pkg")
        self.ctx.add_image(b"\x89PNG\r\n", "image/png")
        name = self.ctx.items[1].name
        self.out._handle_context_href("open:0")
        self.out._handle_context_href("open:1")
        self.assertEqual(self.opened, [])
        self.assertEqual(len(self.revealed), 2)
        self.assertTrue(self.revealed[0].startswith("/tmp/pkg".replace("pkg", "")))
        self.assertEqual(os.path.basename(self.revealed[1]), name)

    def test_a_chip_without_a_path_says_so_and_opens_nothing(self):
        ctx = self.s.context
        ctx.add_path("/tmp/gone.py")
        ctx.items[0].path = ""
        self.out._handle_context_href("open:0")
        self.assertEqual(self.opened, [])
        self.assertEqual(self.rec.messages, ["No path for gone.py"])

    # ── modifier-click removal ──────────────────────────────────────────

    def test_modifier_click_removes_the_chip_and_opens_nothing(self):
        self.ctx.add_file("/tmp/a.py", "a")
        self.ctx.add_file("/tmp/b.py", "b")
        self.out._modifier_held_for_remove = staticmethod(lambda: True)
        self.out._handle_context_href("open:0")
        self.assertEqual([it.path for it in self.ctx.items],
                         ["/tmp/b.py"], "the clicked chip leaves the queue")
        self.assertEqual(self.opened, [], "removal must not also open it")
        self.assertEqual(self.rec.messages, ["Removed context: a.py"])

    def test_modifier_click_is_remove_only_when_the_index_resolves(self):
        self.ctx.add_file("/tmp/a.py", "a")
        self.out._modifier_held_for_remove = staticmethod(lambda: True)
        self.out._handle_context_href("open:4")
        self.assertEqual(len(self.ctx.items), 1)
        self.assertEqual(self.rec.messages, [])

    def test_a_plain_click_still_opens_when_no_modifier_is_held(self):
        self.ctx.add_file("/tmp/a.py", "a")
        self.out._modifier_held_for_remove = staticmethod(lambda: False)
        self.out._handle_context_href("open:0")
        self.assertEqual(self.opened, [("/tmp/a.py", None)])
        self.assertEqual(len(self.ctx.items), 1)

    def test_a_session_without_a_manager_keeps_the_chips_inert(self):
        """Unwired sessions (no ContextManager) must not blow up on a click."""
        plain = make_session(registry=SessionRegistry())
        default_registry.register(plain)
        default_registry.bind(plain.agent_id, self.view.id())
        self.assertIsNone(self.out._context_manager(plain))
        self.out._handle_context_href("clear")
        self.out._handle_context_href("open:0")
        self.assertEqual(self.opened + self.revealed, [])
        self.assertEqual(self.rec.messages, [])

    # ── trailing clear ──────────────────────────────────────────────────

    def test_the_clear_link_drops_every_pending_item(self):
        self.ctx.add_file("/tmp/a.py", "a")
        self.ctx.add_image(b"\x89PNG\r\n", "image/png")
        self.out._handle_context_href("clear")
        self.assertEqual(list(self.s.context.items), [])
        self.assertEqual(self.rec.messages, ["Context cleared"])
        self.assertEqual(self.opened + self.revealed, [])

    # ── frozen transcript chips (turn:) ─────────────────────────────────

    def test_a_frozen_turn_chip_uses_its_stored_ref(self):
        self.out.prompt("p")
        from ui.models import Conversation

        self.out.current = Conversation()
        self.out.current.context_refs = [
            {"name": "x.py:L3-L4", "path": "/tmp/x.py", "line_range": "L3-L4",
             "action": "open", "kind": "selection"},
            {"name": "assets/", "path": "/tmp/assets", "line_range": "",
             "action": "reveal", "kind": "folder"},
        ]
        self.out._handle_context_href("turn:0")
        self.out._handle_context_href("turn:1")
        self.assertEqual(self.opened, [("/tmp/x.py", 3)])
        self.assertEqual(self.revealed, ["/tmp/assets"])

    def test_an_out_of_range_frozen_chip_is_inert(self):
        self.out.prompt("p")
        self.out.current.context_refs = []
        self.out._handle_context_href("turn:3")
        self.assertEqual(self.opened + self.revealed, [])

    # ── the chip html itself ────────────────────────────────────────────

    def test_composer_chip_html_advertises_remove_and_clear(self):
        import ui.composer as composer_mod

        self.ctx.add_folder("/tmp/pkg")
        items = list(self.s.context.items)
        real = composer_mod.sublime
        composer_mod.sublime = self.rec
        try:
            from ui.composer import Composer

            c = Composer(self.out)
            c._pending_context_region = (0, 6)
            self.view._content = "📎 \n"
            c._refresh_context_phantoms(items)
        finally:
            composer_mod.sublime = real
        html = self.rec.phantoms[0]["html"]
        self.assertIn('href="open:0"', html)
        self.assertIn("ctrl/cmd+click to remove", html)
        self.assertIn("click to reveal", html, "a folder chip reveals")
        self.assertIn('href="clear"', html)

    def test_turn_chip_html_advertises_the_action(self):
        import ui.renderer as renderer_mod

        refs = [{"name": "x.py", "path": "/tmp/x.py", "line_range": "L3",
                 "action": "open", "kind": "file"}]
        self.out.prompt("p")
        real = renderer_mod.sublime
        renderer_mod.sublime = self.rec
        try:
            self.view._content = "📎 \n"
            self.out.renderer._refresh_turn_context_phantoms(
                refs, region=(0, 6))
        finally:
            renderer_mod.sublime = real
        html = self.rec.phantoms[0]["html"]
        self.assertIn('href="turn:0"', html)
        self.assertIn("click to open /tmp/x.py", html)

    # ── removal re-renders the chips ────────────────────────────────────

    def test_removal_repaints_the_chip_row(self):
        """`remove_at` must push the shorter list at the composer, or the
        removed chip stays on screen until something else repaints."""
        seen = []
        self.s.output.set_pending_context = lambda items: seen.append(
            [it.name for it in items])
        self.ctx.add_file("/tmp/a.py", "a")
        self.ctx.add_file("/tmp/b.py", "b")
        self.s.context.remove_at(0)
        self.assertEqual(seen[-1], ["b.py"])


if __name__ == "__main__":
    unittest.main()
