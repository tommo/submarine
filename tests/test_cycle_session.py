"""Ctrl+] / Ctrl+[ cycle every current session in this window, sleeping too."""
from __future__ import annotations

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import FakeClient, make_session
from tests.stubs import FakeWindow, install_sublime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _json_file(name):
    kept = []
    for line in open(os.path.join(ROOT, name), encoding="utf-8"):
        if line.strip().startswith("//"):
            continue
        kept.append(re.sub(r"\s+//.*$", "", line))
    return json.loads("\n".join(kept))


class CycleKeymapTest(unittest.TestCase):
    def test_ctrl_brackets_also_bound_on_the_sessions_list(self):
        import json
        with open(os.path.join(ROOT, "Default.sublime-keymap"), encoding="utf-8") as f:
            raw = f.read()
        entries = json.loads("\n".join(
            ln for ln in raw.splitlines() if not ln.lstrip().startswith("//")))
        on_list = [e for e in entries
                   if e.get("command") == "submarine_cycle_session"
                   and any(c.get("key") == "setting.submarine_session_list"
                           and c.get("operator") == "equal" and c.get("operand") is True
                           for c in e.get("context", []))]
        self.assertEqual(sorted(e["keys"][0] for e in on_list), ["ctrl+[", "ctrl+]"])

    def test_ctrl_brackets_cycle_sessions(self):
        keymap = _json_file("Default.sublime-keymap")
        nxt = [e for e in keymap
               if e.get("command") == "submarine_cycle_session"
               and e.get("keys") == ["ctrl+]"]
               and (e.get("args") or {}).get("direction") == 1]
        prv = [e for e in keymap
               if e.get("command") == "submarine_cycle_session"
               and e.get("keys") == ["ctrl+["]
               and (e.get("args") or {}).get("direction") == -1]
        self.assertGreaterEqual(len(nxt), 1)
        self.assertGreaterEqual(len(prv), 1)
        for e in nxt + prv:
            ctx = e.get("context") or []
            self.assertTrue(
                any(c.get("key") in ("setting.submarine_output",
                                     "setting.submarine_session_list") for c in ctx),
                "Ctrl+[ / ] only on a session sheet or the Sessions list")

    def test_palette_lists_next_and_previous(self):
        cmds = _json_file("Default.sublime-commands")
        names = [e.get("command") for e in cmds]
        self.assertEqual(names.count("submarine_cycle_session"), 2)


class AwakeSessionsTest(unittest.TestCase):
    def setUp(self):
        install_sublime()
        from core.registry import default_registry
        default_registry.clear()
        self.reg = default_registry
        self.win = FakeWindow()

    def tearDown(self):
        self.reg.clear()

    def test_includes_sleeping_and_skips_other_windows(self):
        import commands.session_cmds as sc

        other = FakeWindow()
        other._id = 99
        live = make_session(
            window=self.win, initialized=True, client=FakeClient(),
            registry=self.reg)
        live.session_id = "live"
        live.last_access = 2
        asleep = make_session(
            window=self.win, resume_id="sleep", registry=self.reg)
        asleep.last_access = 9
        foreign = make_session(
            window=other, initialized=True, client=FakeClient(),
            registry=self.reg)
        foreign.session_id = "foreign"
        foreign.last_access = 8
        for s in (live, asleep, foreign):
            self.reg.register_session(s)
        ids = [getattr(s, "session_id", None)
               for s in sc.cycle_sessions_for_window(self.win)]
        self.assertEqual(ids, ["sleep", "live"])   # most recently touched first
        self.assertNotIn("foreign", ids)

    def test_cycle_wraps_to_the_other_session(self):
        import commands.session_cmds as sc

        a = make_session(
            window=self.win, initialized=True, client=FakeClient(),
            registry=self.reg)
        a.session_id = "a"
        a.agent_id = "aa"
        a.last_access = 2
        b = make_session(
            window=self.win, initialized=True, client=FakeClient(),
            registry=self.reg)
        b.session_id = "b"
        b.agent_id = "bb"
        b.last_access = 1
        self.reg.register_session(a)
        self.reg.register_session(b)
        sessions = sc.cycle_sessions_for_window(self.win)
        self.assertEqual([s.session_id for s in sessions], ["a", "b"])
        revealed = []
        current = [a]

        def _reveal(self, session, window):
            revealed.append(session.session_id)
            current[0] = session
            return True

        # Patch the class, and put it back: a leak here broke every
        # test_reveal_session case that ran after this file.
        orig_reveal = sc.SubmarineRevealSessionCommand._reveal
        orig_land = sc.SubmarineRevealSessionCommand._land
        sc.SubmarineRevealSessionCommand._reveal = _reveal
        sc.SubmarineRevealSessionCommand._land = lambda *a, **k: None
        orig = sc.get_active_session
        sc.get_active_session = lambda w: current[0]
        try:
            try:
                cmd = sc.SubmarineCycleSessionCommand(self.win)
            except TypeError:
                cmd = sc.SubmarineCycleSessionCommand()
                cmd.window = self.win
            cmd.run(direction=1)
            cmd.run(direction=1)
            cmd.run(direction=-1)
        finally:
            sc.get_active_session = orig
            sc.SubmarineRevealSessionCommand._reveal = orig_reveal
            sc.SubmarineRevealSessionCommand._land = orig_land
        self.assertEqual(revealed, ["b", "a", "b"])


class ListSyncTest(unittest.TestCase):
    """Cycling moves the Sessions list caret (and scroll) to the new row."""

    def test_sync_list_to_session_places_caret_and_shows_row(self):
        import json
        from ui import session_list as sl
        from tests.stubs import install
        sublime = install()
        prev = sl.sublime
        sl.sublime = sublime
        shown, sel = [], []

        class _Sel(list):
            def clear(self): del self[:]
            def add(self, r): self.append(r); sel.append(r)

        class _View(object):
            def __init__(self):
                self._s = {sl.SETTING: True, sl.ROWS_KEY: json.dumps([
                    {"kind": "live", "agent_id": "aa", "session_id": "a", "line": 4},
                    {"kind": "live", "agent_id": "bb", "session_id": "b", "line": 5},
                ])}
                self._sel = _Sel()
            def settings(self):
                d = self._s
                return type("S", (), {"get": lambda _s, k, dflt=None: d.get(k, dflt),
                                      "set": lambda _s, k, v: d.__setitem__(k, v)})()
            def is_valid(self): return True
            def text_point(self, r, c): return r * 100
            def sel(self): return self._sel
            def show(self, pt): shown.append(pt)

        view = _View()
        win = type("W", (), {"views": lambda _s: [view]})()
        target = type("T", (), {"agent_id": "bb", "session_id": "b"})()
        try:
            self.assertTrue(sl.sync_list_to_session(win, target))
        finally:
            sl.sublime = prev
        self.assertEqual(shown, [400])      # line 5 -> row 4 -> text_point(4, 0)
        self.assertEqual(len(sel), 1)


def _inline_sublime():
    """A `sublime` stand-in for session_list: real-ish regions, no timers."""
    import types
    from tests.test_single_view import _Region
    return types.SimpleNamespace(Region=_Region, set_timeout=lambda f, ms=0: f(),
                                 status_message=lambda m: None)


class RevealTailTest(unittest.TestCase):
    """Revealing from the list scrolls the sheet to its tail (attach alone
    restores the old scroll)."""

    def test_reveal_row_scrolls_to_the_tail(self):
        from core.registry import default_registry
        from tests.test_single_view import RecordingWindow, _session
        from ui import session_list as sl
        from ui.host import HostView, set_ui_mode_override
        default_registry.clear(); HostView.reset(); set_ui_mode_override("single")
        try:
            win = RecordingWindow()
            a = _session(win, "A")
            b = _session(win, "B")
            hv = HostView.for_window(win)
            hv.attach(win, a)
            host = hv.host_view(win)
            a.output.prompt("x"); a.output.text("y" * 400); a.output.meta(0.1)
            a.output.exit_input_mode(keep_text=False)
            host.set_viewport_position((0.0, 0.0))
            a.surface = None
            hv.attach(win, b)
            hv.attach(win, a)                       # plain attach: scroll restored as saved
            host.set_viewport_position((0.0, 0.0))
            default_registry.register_session(a)
            prev = sl.sublime
            sl.sublime = _inline_sublime()           # regions + inline set_timeout
            try:
                ok = sl.reveal_row(win, {"kind": "live", "session_id": a.session_id,
                                         "agent_id": a.agent_id, "section": "CURRENT"})
            finally:
                sl.sublime = prev
            self.assertTrue(ok)
            self.assertGreater(host.viewport_position()[1], 0.0, "not scrolled to the tail")
            sel = list(host.sel())
            self.assertTrue(sel and sel[-1].begin() >= a.output.composer._input_start,
                            "caret not at the tail")
        finally:
            default_registry.clear(); HostView.reset()

    def test_enter_in_the_list_hands_focus_and_caret_to_the_composer(self):
        from core.registry import default_registry
        from tests.test_single_view import RecordingWindow, _session
        from ui import session_list as sl
        from ui.host import HostView, set_ui_mode_override
        default_registry.clear(); HostView.reset(); set_ui_mode_override("single")
        try:
            win = RecordingWindow()
            a = _session(win, "A")
            b = _session(win, "B")
            hv = HostView.for_window(win)
            hv.attach(win, a)
            host = hv.host_view(win)
            a.output.prompt("x"); a.output.text("y"); a.output.meta(0.1)
            hv.attach(win, b)
            lst = win.new_file()                          # the Sessions list has focus
            win.focus_view(lst)
            default_registry.register_session(a)
            prev = sl.sublime
            sl.sublime = _inline_sublime()
            try:
                ok = sl.open_row(win, {"kind": "live", "session_id": a.session_id,
                                       "agent_id": a.agent_id, "section": "CURRENT"})
            finally:
                sl.sublime = prev
            self.assertTrue(ok)
            self.assertIs(win.active_view(), host, "focus stayed on the list")
            self.assertTrue(a.output.is_input_mode(), "no composer after Enter")
            sel = list(host.sel())
            self.assertTrue(sel and sel[-1].begin() >= a.output.composer._input_start,
                            "caret parked outside the composer")
        finally:
            default_registry.clear(); HostView.reset()

