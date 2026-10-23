"""A key typed into the composer is never eaten by a sheet shortcut.

Question shortcuts (1-4, `o`, Enter) were gated on `has_question` alone. A
stale flag with the composer open swallowed every `o` and digit the user
typed, and Enter answered the question instead of sending. A shortcut on a
typed key must say how it relates to the composer: `input_mode` false (a
sheet shortcut), `input_mode` true (a composer action), or the no-session
page (`idle`, no composer there). Sending what was typed on a sleeping
sheet is the one composer action gated on something else.
"""
from __future__ import annotations

import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Acts on what the user typed (Enter on a sleeping sheet wakes and sends).
COMPOSER_ACTIONS = {"submarine_submit_input"}


def _keymap():
    kept = []
    for line in open(os.path.join(ROOT, "Default.sublime-keymap"), encoding="utf-8"):
        if line.strip().startswith("//"):
            continue
        kept.append(re.sub(r"\s+//.*$", "", line))
    return json.loads("\n".join(kept))


def _ctx(binding):
    return {c.get("key"): c.get("operand", True) for c in binding.get("context", [])}


def _is_typed(key):
    """A bare character or Enter — what lands in the composer when typed."""
    return len(key) == 1 or key in ("enter", "space")


class TypingGuardTest(unittest.TestCase):
    def test_sheet_shortcuts_on_typed_keys_stay_off_while_typing(self):
        offenders = []
        for b in _keymap():
            ctx = _ctx(b)
            if ctx.get("setting.submarine_output") is not True:
                continue
            if len(b["keys"]) != 1 or not _is_typed(b["keys"][0]):
                continue
            if b["command"] in COMPOSER_ACTIONS:
                continue
            # Explicit either way: off = a sheet shortcut, on = a composer
            # action (e.g. Enter inserting a newline while composing).
            if ctx.get("setting.submarine_input_mode") in (True, False):
                continue
            if ctx.get("setting.submarine_idle") is True:
                continue
            offenders.append("%s -> %s" % (b["keys"][0], b["command"]))
        self.assertEqual(offenders, [], "shortcuts that fire while typing")

    def test_question_keys_still_answer_with_the_composer_closed(self):
        q = [b for b in _keymap() if b["command"] == "submarine_question_key"]
        self.assertEqual(sorted(b["keys"][0] for b in q),
                         sorted(["1", "2", "3", "4", "o", "enter"]))
        for b in q:
            ctx = _ctx(b)
            self.assertIs(ctx.get("setting.submarine_has_question"), True, b["keys"])
            self.assertIs(ctx.get("setting.submarine_input_mode"), False, b["keys"])


if __name__ == "__main__":
    unittest.main()
