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
                any(c.get("key") == "setting.submarine_output" for c in ctx),
                "Ctrl+[ / ] only on a session sheet")

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

        sc.SubmarineRevealSessionCommand._reveal = _reveal
        sc.SubmarineRevealSessionCommand._land = lambda *a, **k: None
        try:
            cmd = sc.SubmarineCycleSessionCommand(self.win)
        except TypeError:
            cmd = sc.SubmarineCycleSessionCommand()
            cmd.window = self.win
        orig = sc.get_active_session
        sc.get_active_session = lambda w: current[0]
        try:
            cmd.run(direction=1)
            cmd.run(direction=1)
            cmd.run(direction=-1)
        finally:
            sc.get_active_session = orig
        self.assertEqual(revealed, ["b", "a", "b"])


if __name__ == "__main__":
    unittest.main()
