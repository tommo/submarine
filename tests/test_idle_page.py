"""The session sheet with no session: the branding page and its hints.

The page replaced a bare `(no session)` line (and, worse, sheets that kept a
dead session's chrome and name). What matters here: it renders with no session
at all, it names only keys that exist, it flags the view so its keys apply to
it and nothing else, and the flag never survives a session binding.
"""
from __future__ import annotations

import json
import os
import re
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.stubs import FakeSettings, install
from ui import idle, keys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def keymap():
    src = open(os.path.join(ROOT, "Default.sublime-keymap")).read()
    kept = []
    for line in src.splitlines():
        if line.strip().startswith("//"):
            continue
        kept.append(re.sub(r"\s+//.*$", "", line))
    return json.loads("\n".join(kept))


class FakeView(object):
    def __init__(self, vid=1):
        self._id = vid
        self._settings = FakeSettings()
        self.text = ""
        self.read_only = False
        self.name = ""
        self.commands = []
        self._sel = []
        self.shown = []

    def id(self):
        return self._id

    def is_valid(self):
        return True

    def settings(self):
        return self._settings

    def set_read_only(self, flag):
        self.read_only = bool(flag)

    def set_name(self, name):
        self.name = name

    def run_command(self, cmd, args=None):
        self.commands.append((cmd, args))
        if cmd == keys.CMD_CLEAR_ALL:
            self.text = ""
        elif cmd == keys.CMD_INSERT and args:
            self.text = str(args.get("text") or "")

    def viewport_extent(self):
        return (640.0, 400.0)

    def em_width(self):
        return 8.0

    def sel(self):
        return self

    def clear(self):
        self._sel = []

    def add(self, region):
        self._sel.append(region)

    def show(self, pt, animate=False):
        self.shown.append(pt)


class IdlePageTest(unittest.TestCase):
    def setUp(self):
        self.sublime = install()
        # ui modules capture `sublime` at import time; tests inject the stub.
        self._prev_sublime = idle.sublime
        idle.sublime = self.sublime
        self.view = FakeView()

    def tearDown(self):
        idle.sublime = self._prev_sublime

    def test_render_writes_the_page_and_flags_the_view(self):
        self.assertTrue(idle.render(self.view, types.SimpleNamespace(
            folders=lambda: ["/work/pil"])))
        text = self.view.text
        self.assertIn(idle.BRAND, text)
        self.assertIn(idle.TAGLINE, text)
        self.assertIn(idle.BOUND_NONE, text)
        self.assertIn("/work/pil", text)
        self.assertTrue(self.view.read_only, "the page is read-only")
        self.assertEqual(self.view.name, "Submarine", "no stale session name")
        self.assertTrue(idle.is_idle(self.view), "keys need this flag")

    def test_every_hint_shows_a_key_and_an_action(self):
        self.sublime.load_settings(keys.OUTPUT_SETTINGS)
        self.view._settings.set("submarine_idle", True)
        text = idle.page_text(types.SimpleNamespace(folders=lambda: ["/p"]), 64)
        for key_name, action in idle.HINTS:
            self.assertIn(key_name, text, "hint key %r missing" % key_name)
            self.assertIn(action.split(" —")[0], text)
        # The first hint names what enter would actually start.
        self.assertIn(idle.new_session_label(), text)

    def test_the_page_names_no_session(self):
        """Nothing on it can be a dead session's identity."""
        text = idle.page_text(types.SimpleNamespace(folders=lambda: ["/p"]), 64)
        for token in ("agent-", "session_", "◇ DS>", "(unnamed)"):
            self.assertNotIn(token, text)

    def test_clear_drops_the_flag(self):
        idle.render(self.view)
        self.assertTrue(idle.is_idle(self.view))
        idle.clear(self.view)
        self.assertFalse(idle.is_idle(self.view))

    def test_render_is_safe_without_a_view(self):
        self.assertFalse(idle.render(None))
        idle.clear(None)   # must not raise

    def test_the_idle_keys_are_really_bound(self):
        entries = keymap()
        idle_keys = {}
        for e in entries:
            ctx = e.get("context") or []
            if any(c.get("key") == "setting.submarine_idle" for c in ctx):
                for k in e.get("keys") or ():
                    idle_keys[k] = e.get("command")
        for key_name, _action in idle.HINTS:
            if key_name in ("enter", "r", "l"):
                self.assertIn(key_name, idle_keys,
                              "hint %r has no binding" % key_name)
        self.assertEqual(idle_keys.get("enter"), "submarine_start")
        self.assertEqual(idle_keys.get("r"), "submarine_resume")
        self.assertEqual(idle_keys.get("l"), "submarine_session_list")
        # Each binding is scoped to an idle *output* sheet.
        for e in entries:
            ctx = e.get("context") or []
            if any(c.get("key") == "setting.submarine_idle" for c in ctx):
                names = [c.get("key") for c in ctx]
                self.assertIn("setting.submarine_output", names)

    def test_the_other_hints_exist_elsewhere_in_the_keymap(self):
        entries = keymap()
        bound = set()
        for e in entries:
            for k in e.get("keys") or ():
                bound.add(k)
        # ⌘\ switches sessions, ctrl+/ opens the terminal: both bound already.
        self.assertIn("super+\\", bound)
        self.assertIn("ctrl+/", bound)

    def test_the_sheet_is_replaced_when_a_session_binds(self):
        """The flag must not outlive the page: a bound sheet would otherwise
        answer enter/r/l with the idle commands."""
        seen = {}

        class Output(object):
            def clear_idle(self):
                seen["cleared"] = True

        session = types.SimpleNamespace(
            agent_id="a1", torn_off=False, output=Output(), window=None)
        host_module = __import__("ui.host", fromlist=["HostView"])
        host = host_module.HostView.__new__(host_module.HostView)
        host.window = None
        host._view = self.view
        host.host_view = lambda window, create_via=None: self.view
        host._paint_bound = lambda session, host_view: None
        host._finish_bound = lambda window, session, host_view, focus=True: None
        host._refresh_list = lambda: None
        with_stub = __import__("core.registry", fromlist=["default_registry"])
        try:
            host._attach(types.SimpleNamespace(folders=lambda: ["/p"]), session)
        except Exception:
            pass
        finally:
            with_stub.default_registry.clear()
        self.assertTrue(seen.get("cleared"), "binding must clear the idle flag")


if __name__ == "__main__":
    unittest.main()
