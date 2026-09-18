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


def plugin_unloaded():
    """End every terminal session when the package unloads.

    A reload replaces ``Terminal._terminals`` while the old PTY's reader and
    renderer threads keep running; the next activation of that view finds no
    terminal for it and starts a second shell in the same buffer. Close each
    live terminal here and mark its view finished / not reactivable, so a
    reload ends terminal sessions instead of leaking one and forking another.
    """
    try:
        from terminal.terminal import Terminal
    except Exception:
        return
    seen = []
    for t in list(Terminal._terminals.values()) + list(Terminal._detached_terminals):
        if any(t is s for s in seen):
            continue
        seen.append(t)
        view = getattr(t, "view", None)
        try:
            if view is not None and view.is_valid():
                view.settings().set("submarine_terminal.finished", True)
                view.settings().set("submarine_terminal.reactivable", False)
        except Exception:
            pass
        try:
            t._done[0] = True   # stop the reader/renderer threads
        except Exception:
            pass
        try:
            if getattr(t, "_adopted", False):
                t.release()     # borrowed pty: never kill it
            else:
                t.kill()
        except Exception:
            pass
    Terminal._terminals.clear()
    del Terminal._detached_terminals[:]
