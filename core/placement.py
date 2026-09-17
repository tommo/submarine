"""Pane-group memory (old session_split). Not conversation forking.

Module imports without sublime. Functions take window/view objects and
lazy-import sublime only if a caller ever needs the ST API — they do
not import it at module level. Window settings keys are `submarine_*`
with a read fallback to the old `claude_*` names.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

_ACTIVE_AGENT = "submarine_active_agent"
_ACTIVE_VIEW = "submarine_active_view"
_ACTIVE_GROUP = "submarine_active_group"
_LEGACY_VIEW = "claude_active_view"
_LEGACY_GROUP = "claude_active_group"

# Sheet tab positions, remembered per window and per view kind (group +
# index). The session sheet and the session list usually live in different
# splits, so they are tracked independently.
TAB_SESSION = "session"
TAB_LIST = "list"
_TABS = "submarine_tabs"
_LEGACY_TABS = "claude_tabs"
_LEGACY_SESSION_TAB = "claude_session_tab"
_WINDOW_TABS_FILE = "window_tabs.json"
_WINDOW_TABS_CAP = 40


def view_tab_kind(view) -> Optional[str]:
    # type: (Any) -> Optional[str]
    """Which remembered slot this view owns, if any.

    `session` is the agent output sheet, `list` the Sessions scratch view.
    """
    try:
        st = view.settings()
    except Exception:
        return None
    if st.get("submarine_output") or st.get("claude_output"):
        return TAB_SESSION
    if st.get("submarine_session_list") or st.get("claude_session_list"):
        return TAB_LIST
    return None


def _window_tabs_path() -> str:
    try:
        from plat.constants import USER_PROFILES_DIR
        base = str(USER_PROFILES_DIR)
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".submarine")
    return os.path.join(base, _WINDOW_TABS_FILE)


def window_tab_key(window) -> str:
    # type: (Any) -> str
    """Stable per-window identity for the remembered tab position.

    The workspace file is stable across restarts and unique per window; the
    project file and the folder list are fallbacks for windows without one,
    and the runtime id is the last resort (memory only, not restart-surviving).
    """
    if window is None:
        return ""
    for getter in ("workspace_file_name", "project_file_name"):
        try:
            fn = getattr(window, getter, None)
            val = fn() if callable(fn) else None
            if val:
                return str(val)
        except Exception:
            pass
    try:
        folders = sorted(f for f in (window.folders() or []) if f)
    except Exception:
        folders = []
    if folders:
        return "folders:" + "|".join(folders)
    try:
        return "window:%s" % window.id()
    except Exception:
        return ""


def load_window_tabs() -> dict:
    # type: () -> dict
    try:
        from plat.jsonio import safe_json_load
        data = safe_json_load(_window_tabs_path(), default={})
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_window_tabs(tabs: dict) -> bool:
    # type: (dict) -> bool
    """Write the tab memory, keeping the most recent windows only."""
    try:
        from plat.jsonio import safe_json_dump
    except Exception:
        return False
    if not isinstance(tabs, dict):
        return False
    rows = [
        (k, v) for k, v in tabs.items()
        if isinstance(v, dict) and isinstance(v.get("ts"), (int, float))
    ]
    if len(rows) > _WINDOW_TABS_CAP:
        rows.sort(key=lambda kv: kv[1].get("ts") or 0, reverse=True)
        rows = rows[:_WINDOW_TABS_CAP]
    pruned = {k: v for k, v in rows}
    path = _window_tabs_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    except Exception:
        pass
    try:
        return bool(safe_json_dump(pruned, path))
    except Exception:
        return False


def _tab_layout(window, kind):
    # type: (Any, str) -> Optional[dict]
    """Remembered layout: this window's memory, else the persisted entry."""
    try:
        settings = window.settings()
        tabs = settings.get(_TABS) or settings.get(_LEGACY_TABS) or {}
    except Exception:
        tabs = {}
    entry = tabs.get(kind) if isinstance(tabs, dict) else None
    if isinstance(entry, dict):
        return entry
    if kind == TAB_SESSION:
        # Pre-kind memory lived under its own window setting.
        for legacy_key in ("submarine_session_tab", _LEGACY_SESSION_TAB):
            try:
                legacy = settings.get(legacy_key)
            except Exception:
                legacy = None
            if isinstance(legacy, dict):
                return legacy
    key = window_tab_key(window)
    if not key:
        return None
    row = load_window_tabs().get(key)
    if not isinstance(row, dict):
        return None
    nested = row.get(kind)
    if isinstance(nested, dict):
        return nested
    # Legacy flat entry ({group, index, ts}) was the session sheet's slot.
    if kind == TAB_SESSION and "group" in row:
        return row
    return None


def remember_view_tab(window, view, kind=None) -> bool:
    # type: (Any, Any, Optional[str]) -> bool
    """Remember which slot this window keeps `view` in.

    Called when a tracked view is focused, so a dragged tab is captured without
    a move event (Sublime has none for view drags).
    """
    if not window or not view:
        return False
    kind = kind or view_tab_kind(view)
    if not kind:
        return False
    try:
        group, index = window.get_view_index(view)
    except Exception:
        return False
    if group is None or group < 0:
        return False
    try:
        index = int(index)
    except (TypeError, ValueError):
        index = 0
    if index < 0:
        index = 0
    layout = {"group": int(group), "index": index, "ts": time.time()}
    try:
        tabs = dict(window.settings().get(_TABS) or {})
        tabs[kind] = layout
        window.settings().set(_TABS, tabs)
    except Exception:
        pass
    key = window_tab_key(window)
    if key:
        rows = load_window_tabs()
        row = rows.get(key)
        row = dict(row) if isinstance(row, dict) else {}
        if kind == TAB_SESSION and "group" in row and not isinstance(
                row.get(TAB_SESSION), dict):
            row = {TAB_SESSION: {"group": row.get("group"),
                                 "index": row.get("index"),
                                 "ts": row.get("ts", layout["ts"])}}
        row[kind] = layout
        row["ts"] = layout["ts"]
        rows[key] = row
        save_window_tabs(rows)
    return True


def remember_session_tab(window, view) -> bool:
    # type: (Any, Any) -> bool
    """Remember the agent output sheet's slot (see `remember_view_tab`)."""
    if view_tab_kind(view) != TAB_SESSION:
        return False
    return remember_view_tab(window, view, TAB_SESSION)


def apply_view_tab(window, view, kind=None) -> bool:
    # type: (Any, Any, Optional[str]) -> bool
    """Put `view` back in the slot this window last kept it in."""
    if not window or not view:
        return False
    kind = kind or view_tab_kind(view)
    if not kind:
        return False
    layout = _tab_layout(window, kind)
    if not isinstance(layout, dict):
        return False
    try:
        group = int(layout.get("group", 0))
        index = int(layout.get("index", 0))
    except (TypeError, ValueError):
        return False
    try:
        n_groups = window.num_groups()
        if n_groups <= 0:
            return False
        if group < 0 or group >= n_groups:
            group = window.active_group()
        n_in = len(window.views_in_group(group))
        # An empty group is a legitimate target: a sheet is often alone in its
        # own split, so its remembered slot is the only one.
        index = max(0, min(index, n_in - 1)) if n_in > 0 else 0
        cur_group, cur_index = window.get_view_index(view)
        if cur_group == group and cur_index == index:
            return False
        window.set_view_index(view, group, index)
        return True
    except Exception:
        return False


def apply_session_tab(window, view) -> bool:
    # type: (Any, Any) -> bool
    """Put the agent output sheet back in its remembered slot."""
    if view_tab_kind(view) != TAB_SESSION:
        return False
    return apply_view_tab(window, view, TAB_SESSION)


def _is_session_view(view) -> bool:
    try:
        if not view.is_valid():
            return False
        settings = view.settings()
        return bool(
            settings.get("submarine_output") or settings.get("claude_output")
        )
    except Exception:
        return False


def remember_active_session(window, view) -> None:
    """Record last focused session + its split so resume can land there."""
    if not window or not view:
        return
    if not _is_session_view(view):
        return
    aid = None
    try:
        from core.registry import default_registry
        s = default_registry.for_view(view)
        if s is not None:
            aid = getattr(s, "agent_id", None)
    except Exception:
        aid = None
    if not aid:
        try:
            st = view.settings()
            aid = st.get("submarine_agent_id") or st.get("claude_agent_id")
        except Exception:
            aid = None
    if aid:
        window.settings().set(_ACTIVE_AGENT, aid)
    try:
        group, _index = window.get_view_index(view)
    except Exception:
        return
    if group is None or group < 0:
        return
    window.settings().set(_ACTIVE_GROUP, int(group))


def last_session_group(window) -> Optional[int]:
    """Group index of the last active session sheet, if still valid."""
    if not window:
        return None
    settings = window.settings()
    aid = settings.get(_ACTIVE_AGENT)
    if aid:
        try:
            from core.registry import default_registry
            s = default_registry.by_agent_id(aid)
            v = None
            if s is not None:
                v = getattr(getattr(s, "output", None), "view", None)
            if v is not None and v.is_valid():
                group, _ = window.get_view_index(v)
                if group is not None and group >= 0:
                    return int(group)
        except Exception:
            pass
        try:
            for v in window.views():
                if not v.is_valid():
                    continue
                st = v.settings()
                got = st.get("submarine_agent_id") or st.get("claude_agent_id")
                if got == aid:
                    group, _ = window.get_view_index(v)
                    if group is not None and group >= 0:
                        return int(group)
        except Exception:
            pass
    vid = settings.get(_ACTIVE_VIEW)
    if vid is None:
        vid = settings.get(_LEGACY_VIEW)
    if vid is not None:
        try:
            for v in window.views():
                if v.id() == vid and v.is_valid():
                    group, _ = window.get_view_index(v)
                    if group is not None and group >= 0:
                        return int(group)
        except Exception:
            pass
    group = settings.get(_ACTIVE_GROUP)
    if group is None:
        group = settings.get(_LEGACY_GROUP)
    try:
        group = int(group)
    except (TypeError, ValueError):
        return None
    try:
        n = window.num_groups()
    except Exception:
        return None
    if n <= 0 or group < 0 or group >= n:
        return None
    return group


def open_file_in_last_session_split(window, path: str):
    """Open a file read-write in the last session split, word-wrapped.

    Mirrors ui/modals._open_plan_file. Safe when sublime is missing
    (tests): uses window.open_file if present.
    """
    if not window or not path:
        return None
    view = None
    try:
        view = window.open_file(path)
    except Exception:
        view = None
    if view is not None:
        try:
            place_in_last_session_split(window, view)
        except Exception:
            pass
        try:
            st = view.settings()
            st.set("word_wrap", True)
        except Exception:
            pass
        try:
            import sublime
        except ImportError:
            sublime = None  # type: ignore
        if sublime is not None and getattr(view, "is_loading", None):

            def enable_wrap(v=view):
                try:
                    if v.is_loading():
                        sublime.set_timeout(lambda: enable_wrap(v), 100)
                        return
                    v.settings().set("word_wrap", True)
                except Exception:
                    pass

            try:
                sublime.set_timeout(enable_wrap, 0)
            except Exception:
                pass
    return view


def place_in_last_session_split(window, view) -> bool:
    """Move a new sheet into the last active session's split."""
    if not window or not view:
        return False
    group = last_session_group(window)
    if group is None:
        return False
    try:
        if not view.is_valid():
            return False
        cur, _ = window.get_view_index(view)
        if cur == group:
            return False
        n_in = len(window.views_in_group(group))
        window.set_view_index(view, group, n_in)
        return True
    except Exception:
        return False
