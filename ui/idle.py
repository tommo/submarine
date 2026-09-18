"""The session sheet with no session: a branding page and the ways out.

A Submarine sheet outlives the session it showed — you stop a session, you
restart the plugin, you tear a session off with nothing to bind in its place.
Until now those sheets kept whatever the dead session had left in the buffer
(a stale `📎` chip line, a name from a session that no longer exists) and
swallowed every key. This module is the one writer for that state: a short
page naming the window's project, what a new session would start, and the keys
that get you out.

Rules it follows:
- chrome only: no session, no bridge, nothing is started or stopped here.
- `render()` is the only writer while unbound, and it flags the view
  (`keys.IDLE`) so the idle keybindings apply to it and nothing else.
- anything that binds a session to the sheet calls `clear()` first, so the flag
  can never outlive the page.
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from plat.constants import SETTINGS_FILE
from ui import keys

try:
    import sublime
except Exception:  # pragma: no cover - tests run without the ST runtime
    sublime = None

BRAND = "◇ Submarine"
TAGLINE = "agent sessions in Sublime Text"
BOUND_NONE = "Nothing is bound to this sheet."

# (key, what it does). The first three are bound in Default.sublime-keymap
# under the idle context; the last two already exist for every sheet.
HINTS: List[Tuple[str, str]] = [
    ("enter", "new session"),
    ("r", "resume a session…"),
    ("l", "session list"),
    ("⌘\\", "switch session"),
    ("ctrl+/", "terminal"),
    ("⇧⌘P", "Command Palette → Submarine: …"),
]

MIN_WIDTH = 46
MAX_WIDTH = 96


def is_idle(view: Any) -> bool:
    """Is this sheet showing the page (rather than a session)?"""
    try:
        return bool(keys.read_setting(view.settings(), keys.IDLE))
    except Exception:
        return False


def clear(view: Any) -> None:
    """Call before a session takes the sheet over."""
    if view is None:
        return
    try:
        keys.erase_setting(view.settings(), keys.IDLE)
    except Exception:
        pass


def _settings() -> Any:
    try:
        return sublime.load_settings(SETTINGS_FILE)
    except Exception:
        return None


def new_session_label() -> str:
    """What `enter` would start, e.g. `claude · opus`.

    Read from settings, never guessed: the default backend, and the model
    configured for it when there is one.
    """
    st = _settings()
    backend = ""
    model = ""
    try:
        backend = str(st.get("default_backend", "claude") or "claude")
    except Exception:
        backend = "claude"
    try:
        per_backend = st.get("default_models")
        if isinstance(per_backend, dict):
            model = str(per_backend.get(backend) or "")
    except Exception:
        model = ""
    if not model:
        try:
            if backend == "claude":
                model = str(st.get("default_model") or "")
        except Exception:
            model = ""
    return " · ".join(part for part in (backend, model) if part)


def _project(window: Any) -> str:
    try:
        folders = window.folders() if window is not None else None
        if folders:
            return str(folders[0])
    except Exception:
        pass
    return ""


def _mode() -> str:
    try:
        from ui.host import is_single_mode
        return "single mode" if is_single_mode() else "tabs"
    except Exception:
        return ""


def page_text(window: Any = None, width: int = 64) -> str:
    """The page. Pure: no view, no side effects (tests read it directly)."""
    width = max(MIN_WIDTH, min(MAX_WIDTH, int(width or 0) or 64))
    footer = " · ".join(p for p in (_project(window), _mode()) if p)
    label = new_session_label()
    lines = [BRAND, TAGLINE, "", BOUND_NONE, ""]
    pad = max(len(k) for k, _ in HINTS)
    for key_name, action in HINTS:
        if key_name == "enter" and label:
            action = "%s — %s" % (action, label)
        lines.append("  %s  %s" % (key_name.ljust(pad), action))
    if footer:
        lines.extend(["", footer])
    return "\n".join(lines) + "\n"


def render(view: Any, window: Any = None) -> bool:
    """Write the page into `view` and flag it idle. Viewless: no-op.

    The caret/scroll chrome needs `sublime`; the page itself does not, so a
    host without the runtime still gets text and the flag.
    """
    if view is None:
        return False
    try:
        if not view.is_valid():
            return False
    except Exception:
        return False
    try:
        width = int(view.viewport_extent()[0] / (view.em_width() or 8.0)) - 2
    except Exception:
        width = 0
    text = page_text(window, width)
    try:
        view.set_read_only(False)
        view.run_command(keys.CMD_CLEAR_ALL)
        view.run_command(keys.CMD_INSERT, {"pos": 0, "text": text})
        view.set_read_only(True)
        keys.write_setting(view.settings(), keys.IDLE, True)
    except Exception:
        return False
    if sublime is not None:
        try:
            view.sel().clear()
            view.sel().add(sublime.Region(0, 0))
            view.show(0, False)
        except Exception:
            pass
    try:
        view.set_name("Submarine")
    except Exception:
        pass
    return True
