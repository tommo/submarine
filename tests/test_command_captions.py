"""Palette captions use one verb per intent.

The palette is read as a list: "Select Model" next to "Change Provider for
Current Session…", "View Session History..." next to "Show Usage", and three
different spellings of an ellipsis made related commands look unrelated.
These pin the scheme instead of the individual strings:

  New / Open / Reveal / Show / Select / Set Default / Edit / Clear / Toggle /
  Manage / Copy, plus the session verbs (Start, Stop, Sleep, Wake, Restart,
  Fork, Rename, Resume, Hide, Tear Off, Dock, Query, Queue, Send, Interrupt,
  Undo, Search, Refresh, Generate, Add, Pause, Devtools, Next/Previous).

`…` (one character, never "...") marks — and only marks — a command that
opens another picker or an input panel.
"""
from __future__ import annotations

import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The verb each caption may start with, after the "Submarine: " prefix.
VERBS = {
    "Add", "Clear", "Copy", "Devtools", "Dock", "Edit", "Fork", "Generate",
    "Hide", "Interrupt", "Manage", "New", "Next", "Open", "Pause", "Previous",
    "Query", "Queue", "Refresh", "Rename", "Reset", "Restart", "Resume",
    "Reveal", "Search", "Select", "Send", "Session", "Set", "Show", "Sleep",
    "Stop", "Switch", "Tear", "Toggle", "Undo", "Wake",
}
# Commands whose caption is a plain noun by design.
NOUN_CAPTIONS = {"Submarine: Session List"}
# Commands that open a picker only in some configurations (New Session shows
# one when the project has profiles), so the caption cannot promise either.
CONDITIONAL_PICKERS = {"submarine_start"}


def _entries():
    path = os.path.join(ROOT, "Default.sublime-commands")
    with open(path, encoding="utf-8") as f:
        return [e for e in json.load(f) if e.get("caption", "-") != "-"]


def _opens_panel(command):
    """True when the command shows a quick panel or an input panel."""
    cls = "".join(p.capitalize() for p in command.split("_")) + "Command"
    for folder in ("commands", "ui", "features"):
        base = os.path.join(ROOT, folder)
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(base, name), encoding="utf-8") as f:
                src = f.read()
            m = re.search(r"class %s\(.*?\):(.*?)(?=\nclass |\Z)" % cls, src, re.S)
            if m:
                body = m.group(1)
                return ("show_quick_panel" in body
                        or "show_input_panel" in body)
    return None          # command lives elsewhere (ST built-in, dynamic)


class CaptionSchemeTest(unittest.TestCase):
    def test_every_caption_starts_with_a_known_verb(self):
        bad = []
        for e in _entries():
            cap = e["caption"]
            if cap in NOUN_CAPTIONS or not cap.startswith("Submarine: "):
                continue          # backend entries: "Grok: New Session"
            first = cap[len("Submarine: "):].split(" ")[0].strip("…")
            if first not in VERBS:
                bad.append(cap)
        self.assertEqual(bad, [], "captions not starting with a shared verb")

    def test_no_three_dot_ellipsis(self):
        bad = [e["caption"] for e in _entries() if "..." in e["caption"]]
        self.assertEqual(bad, [], 'use "…", not "..."')

    def test_the_ellipsis_marks_a_picker(self):
        wrong = []
        for e in _entries():
            cap, cmd = e["caption"], e.get("command", "")
            if cmd in CONDITIONAL_PICKERS:
                continue
            opens = _opens_panel(cmd)
            if opens is None:
                continue
            # A trailing shortcut hint — "Switch Session… (⌘\\)" — follows the
            # ellipsis, so the mark is anywhere in the caption.
            marked = "…" in cap
            if opens and not marked:
                wrong.append("%s opens a picker without …" % cap)
            if not opens and marked:
                wrong.append("%s ends with … but opens nothing" % cap)
        self.assertEqual(wrong, [])

    def test_one_verb_per_intent(self):
        caps = [e["caption"] for e in _entries()]
        joined = "\n".join(caps)
        # Reading something back: Show, never View.
        self.assertNotIn("Submarine: View ", joined)
        # Picking a value for the current session: Select.
        for what in ("Model…", "Effort…", "Provider…", "Permission Mode…"):
            self.assertIn("Submarine: Select %s" % what, caps)
        # Defaults for the next session: Set Default.
        for what in ("Model…", "Provider…"):
            self.assertIn("Submarine: Set Default %s" % what, caps)


if __name__ == "__main__":
    unittest.main()
