"""⌥⌘\\ reveals the window's session view — the reverse of Hide Session.

Hiding closes the sheet and leaves the session running ("open from Sessions"),
so the way back has to work from any view in the window — with no context to
match, since the output view it would key off is exactly what is gone — must not
take the terminal's backslash chords away from the PTY, and in single-view mode
must land on the window's existing sheet instead of opening a second one.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.stubs import FakeSettings, install_sublime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVEAL = "submarine_reveal_session"


def _json_file(name):
    kept = []
    for line in open(os.path.join(ROOT, name), encoding="utf-8"):
        if line.strip().startswith("//"):
            continue
        kept.append(re.sub(r"\s+//.*$", "", line))
    return json.loads("\n".join(kept))


class KeymapTest(unittest.TestCase):
    def setUp(self):
        self.keymap = _json_file("Default.sublime-keymap")

    def _entries(self, command):
        return [e for e in self.keymap if e.get("command") == command]

    def test_cmd_alt_backslash_reveals_the_session(self):
        hits = [e for e in self._entries(REVEAL) if e.get("keys") == ["super+alt+\\"]]
        self.assertEqual(len(hits), 1, "exactly one ⌥⌘\\ binding")

    def test_it_carries_no_context(self):
        """Every context that could gate it names something the hidden state
        does not have (`submarine_output`, `submarine_terminal`); a gate here
        would silently swallow the chord."""
        for entry in self._entries(REVEAL):
            self.assertFalse(entry.get("context"))

    def test_it_no_longer_shadows_the_legacy_switch_alias(self):
        hits = [e for e in self._entries("submarine_switch")
                if e.get("keys") == ["super+alt+\\"]]
        self.assertEqual(hits, [], "the alias had to give up the chord")
        still = [e.get("keys") for e in self._entries("submarine_switch")]
        self.assertIn(["super+\\"], still, "switch keeps ⌘\\")

    def test_the_terminal_keeps_its_backslash_chords(self):
        plain = [e for e in self._entries("submarine_terminal_keypress")
                 if e.get("keys") == ["\\"]]
        ctrl = [e for e in self._entries("submarine_terminal_keypress")
                if e.get("keys") == ["ctrl+\\"]]
        self.assertEqual(len(plain), 1, "the PTY binding must survive")
        self.assertEqual(len(ctrl), 1, "the PTY binding must survive")
        self.assertEqual([c.get("key") for c in plain[0].get("context") or []],
                         ["submarine_terminal"])

    def test_palette_and_menu_point_at_a_real_command(self):
        cmds = [e.get("command") for e in _json_file("Default.sublime-commands")]
        self.assertIn(REVEAL, cmds)
        menu = json.dumps(_json_file("Main.sublime-menu"))
        self.assertIn(REVEAL, menu)

    def test_the_command_exists(self):
        install_sublime()
        import commands.session_cmds as sc

        self.assertTrue(hasattr(sc, "SubmarineRevealSessionCommand"))

    def test_the_command_is_reexported_for_st(self):
        """ST only registers Command subclasses that reach submarine.py."""
        init_src = open(os.path.join(ROOT, "commands", "__init__.py"),
                        encoding="utf-8").read()
        root_src = open(os.path.join(ROOT, "submarine.py"), encoding="utf-8").read()
        self.assertIn("SubmarineRevealSessionCommand", init_src)
        self.assertIn("SubmarineRevealSessionCommand", root_src)


class _View(object):
    def __init__(self, vid=7, output=True, size=40):
        self._id = vid
        self._settings = FakeSettings()
        self._settings.set("submarine_output", output)
        self._size = size
        self.shown = []

    def id(self):
        return self._id

    def settings(self):
        return self._settings

    def size(self):
        return self._size

    def show(self, region, show_surrounds=True):
        self.shown.append((region, show_surrounds))


class _Window(object):
    def __init__(self, views=(), wid=1):
        self._views = list(views)
        self.focused = []
        self.front = 0
        self._id = wid

    def views(self):
        return list(self._views)

    def focus_view(self, view):
        self.focused.append(view.id())

    def bring_to_front(self):
        self.front += 1

    def id(self):
        return self._id


class _Output(object):
    def __init__(self, view=None, input_mode=True):
        self.view = view
        self.input_mode = input_mode
        self.focuses = []

    def is_input_mode(self):
        return self.input_mode

    def focus_composer(self, force_show=False, steal_focus=False,
                       preserve_caret=False, park_at_end=False):
        self.focuses.append({
            "force_show": force_show,
            "steal_focus": steal_focus,
            "park_at_end": park_at_end,
        })


class _Session(object):
    def __init__(self, view=None, torn_off=False, window=None, working=False):
        self.agent_id = "agent-x"
        self.output = _Output(view)
        self.torn_off = torn_off
        self.window = window
        self.last_access = 0.0
        self.stopped = False
        self.detached = False
        self.working = working
        self.entered = 0

    def stop(self):
        self.stopped = True

    def _enter_input_with_draft(self):
        self.entered += 1
        self.output.input_mode = True


class _Harness(unittest.TestCase):
    """Fake window + session, recording what the command attaches or creates."""

    def setUp(self):
        install_sublime()
        import commands.session_cmds as sc
        import ui.host as host
        import ui.session_list as sl

        self.sc = sc
        self.host = host
        self.sl = sl
        self.window = _Window([_View(7), _View(9, output=False)])
        self.session = _Session(_View(7))
        self.attaches = []
        self.reveals = []
        self.messages = []

        self._get = sc.get_active_session
        self._single = host.is_single_mode
        self._for_window = host.HostView.for_window
        self._reveal = sl.reveal_live_session
        self._status = sc.sublime.status_message

        sc.get_active_session = lambda win: self.session

        class _Host(object):
            def attach(_self, win, session, focus=True):
                self.attaches.append((session, focus))
                return True

        host.HostView.for_window = staticmethod(lambda win: _Host())
        host.is_single_mode = lambda: True
        sl.reveal_live_session = (
            lambda win, session, focus=True, force_sheet=False:
            (self.reveals.append((session, focus, force_sheet)), True)[1])
        self.bottoms = []
        self._bottom = sl.reveal_session_bottom
        sl.reveal_session_bottom = (
            lambda session: self.bottoms.append(session))
        sc.sublime.status_message = self.messages.append
        # The command traces into the devtools ring (the log a dead chord gets
        # diagnosed from): record it instead of writing the live log.
        import features.devtools.server as devtools

        self._devtools = devtools
        self._log = devtools.log
        self.traces = []
        devtools.log = (
            lambda msg, level="info", **fields:
            self.traces.append((msg, level, fields)))

    def tearDown(self):
        self._devtools.log = self._log
        self.sc.get_active_session = self._get
        self.host.is_single_mode = self._single
        self.host.HostView.for_window = self._for_window
        self.sl.reveal_live_session = self._reveal
        self.sl.reveal_session_bottom = self._bottom
        self.sc.sublime.status_message = self._status

    def _run(self):
        cmd = self.sc.SubmarineRevealSessionCommand()
        cmd.window = self.window
        cmd.run()
        return cmd

class CommandTest(_Harness):
    def test_single_view_reattaches_the_window_sheet(self):
        """The hidden session comes back onto the sheet that is already there
        (or onto the idle page's sheet) — never a second sheet."""
        self._run()
        self.assertEqual(self.attaches, [(self.session, True)])
        self.assertEqual(self.reveals, [], "no new sheet in single mode")
        self.assertFalse(self.session.stopped)
        self.assertEqual([t[0] for t in self.traces], ["reveal session: revealed"],
                         "a press must leave a trace to diagnose from")

    def test_tabs_mode_reattaches_a_detached_sheet(self):
        self.host.is_single_mode = lambda: False
        self._run()
        self.assertEqual(self.attaches, [])
        self.assertEqual(self.reveals, [(self.session, True, True)])

    def test_a_torn_off_session_keeps_its_own_sheet(self):
        self.session.torn_off = True
        self._run()
        self.assertEqual(self.attaches, [])
        self.assertEqual(self.reveals, [(self.session, True, True)])

    def test_a_failed_attach_falls_back_to_a_sheet(self):
        class _Host(object):
            def attach(_self, win, session, focus=True):
                return False

        self.host.HostView.for_window = staticmethod(lambda win: _Host())
        self._run()
        self.assertEqual(self.reveals, [(self.session, True, True)])

    def test_no_session_focuses_an_existing_submarine_sheet(self):
        self.sc.get_active_session = lambda win: None
        win = _Window([_View(9, output=False), _View(3)])
        self.window._views = win._views
        self._run()
        self.assertEqual(self.window.focused, [9], "the Submarine sheet, if any")
        self.assertEqual(self.messages, [])

    def test_no_session_anywhere_says_so(self):
        self.sc.get_active_session = lambda win: None
        self.window._views = [_View(3, output=False)]
        self._run()
        self.assertEqual(self.window.focused, [])
        self.assertEqual(len(self.messages), 1)
        self.assertIn("no session", self.messages[0])
        self.assertEqual([t[1] for t in self.traces], ["warn"])

    def test_it_is_always_available(self):
        cmd = self.sc.SubmarineRevealSessionCommand()
        cmd.window = self.window
        self.assertTrue(cmd.is_enabled())

    def test_idle_lands_in_the_composer(self):
        self._run()
        self.assertEqual(self.session.entered, 1)
        self.assertEqual(self.session.output.focuses, [{
            "force_show": True,
            "steal_focus": True,
            "park_at_end": True,
        }])
        self.assertEqual(self.bottoms, [])

    def test_no_composer_lands_at_the_tail(self):
        self.session.working = True
        self.session.output.input_mode = False
        self._run()
        self.assertEqual(self.session.entered, 0)
        self.assertEqual(self.session.output.focuses, [])
        self.assertEqual(self.bottoms, [self.session])

    def test_no_session_sheet_shows_the_tail(self):
        self.sc.get_active_session = lambda win: None
        sheet = _View(9)
        self.window._views = [sheet]
        self._run()
        self.assertEqual(self.window.focused, [9])
        self.assertTrue(sheet.shown, "viewport at the tail of the idle sheet")


class _Registry(object):
    def __init__(self, sessions):
        self._sessions = sessions

    def iter_sessions(self):
        return list(self._sessions)


class OtherWindowTest(_Harness):
    """A session lives in the window that opened it: from a code window the
    useful answer is "show me my session", not "there is no session here"."""

    def setUp(self):
        super(OtherWindowTest, self).setUp()
        self.window._id = 2
        self.other = _Window([_View(9, output=False)], wid=3)
        self.other_session = _Session(_View(9), window=self.other)
        self.other_session.agent_id = "agent-other"
        self.other_session.last_access = 500.0
        import core.registry as reg

        self.reg = reg
        self._default = reg.default_registry
        reg.default_registry = _Registry([self.other_session])
        self._windows = self.sc.sublime.windows
        self.sc.sublime.windows = lambda: [self.window, self.other]
        self.sc.get_active_session = lambda win: None
        # the reveal must land in the OTHER window, so route by window id
        self.attached_in = []

        class _Host(object):
            def __init__(_self, win):
                _self.win = win

            def attach(_self, win, session, focus=True):
                self.attached_in.append((win.id(), session.agent_id, focus))
                return True

        self.host.HostView.for_window = staticmethod(lambda win: _Host(win))

    def tearDown(self):
        self.reg.default_registry = self._default
        self.sc.sublime.windows = self._windows
        super(OtherWindowTest, self).tearDown()

    def test_the_other_window_is_raised_and_the_session_revealed_there(self):
        self._run()
        self.assertEqual(self.other.front, 1, "the session's window comes forward")
        self.assertEqual(self.attached_in, [(3, "agent-other", True)])
        self.assertEqual(self.messages, [])

    def test_this_window_wins_when_it_has_a_session(self):
        self.sc.get_active_session = lambda win: self.session
        self._run()
        self.assertEqual(self.other.front, 0, "no cross-window search needed")
        self.assertEqual(self.attached_in, [(2, "agent-x", True)],
                         "revealed here, not in the other window")

    def test_a_dead_window_record_is_ignored(self):
        self.sc.sublime.windows = lambda: [self.window]
        self.window._views = [_View(9, output=False)]
        self._run()
        self.assertEqual(self.attached_in, [])
        self.assertEqual(len(self.messages), 1)
        self.assertIn("no session", self.messages[0])

    def test_the_most_recently_active_session_wins(self):
        newer = _Session(_View(11), window=self.other)
        newer.agent_id = "agent-newer"
        newer.last_access = 900.0
        self.reg.default_registry = _Registry([self.other_session, newer])
        self._run()
        self.assertEqual(self.attached_in, [(3, "agent-newer", True)])


if __name__ == "__main__":
    unittest.main()
