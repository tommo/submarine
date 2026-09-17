"""Re-export the embedded terminal's commands so Sublime Text discovers them.

ST only auto-loads ROOT ``*.py`` files, so the terminal/ package's commands and
its event listener surface from here (vendored Terminus-derived emulator, see
terminal/LICENSE_TERMINUS). Also generates the color scheme the renderer colors
cells against.
"""
from __future__ import annotations

import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from terminal.view import (  # noqa: F401
    SubmarineTerminalInsertCommand,
    SubmarineTerminalTrimTrailingLinesCommand,
    SubmarineTerminalNukeCommand,
)
from terminal.render import (  # noqa: F401
    SubmarineTerminalRenderCommand,
    SubmarineTerminalShowCursorCommand,
    SubmarineTerminalCleanupCommand,
)
from terminal.commands import (  # noqa: F401
    SubmarineTerminalOpenCommand,
    SubmarineTerminalKeypressCommand,
    SubmarineTerminalPasteCommand,
    SubmarineTerminalCopyCommand,
    SubmarineTerminalOpenLinkCommand,
    SubmarineTerminalSendStringCommand,
    SubmarineTerminalCloseCommand,
    SubmarineTerminalResetCommand,
    SubmarineTerminalActivateCommand,
    SubmarineTerminalPasteTextCommand,
    SubmarineTerminalClearUndoStackCommand,
    SubmarineTerminalScrollCommand,
    SubmarineTerminalAdjustFontSizeCommand,
    NoopCommand,
)
from terminal.event import SubmarineTerminalEventListener  # noqa: F401


def plugin_loaded():
    """Write the terminal color scheme into User/ once.

    16 ANSI + 256 palette, foreground-only; 24-bit truecolor is approximated to
    the nearest 256 color at render time. Lives in User/ so it is not a repo
    artifact.
    """
    try:
        import sublime
    except ImportError:
        return
    try:
        path = os.path.join(
            sublime.packages_path(), "User",
            "SubmarineTerminal.hidden-color-scheme")
        if os.path.isfile(path):
            return
        from terminal.theme_generator import generate_theme_file
        generate_theme_file(path, foreground_only=True)
        print("[Submarine] terminal color scheme: %s" % path)
    except Exception as e:
        print("[Submarine] terminal color scheme failed: %s" % e)
