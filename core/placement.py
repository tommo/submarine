"""Pane-group memory (old session_split). Not conversation forking.

Module imports without sublime. Functions take window/view objects and
lazy-import sublime only if a caller ever needs the ST API — they do
not import it at module level. Window settings keys are `submarine_*`
with a read fallback to the old `claude_*` names.
"""
from __future__ import annotations

from typing import Optional

_ACTIVE_AGENT = "submarine_active_agent"
_ACTIVE_VIEW = "submarine_active_view"
_ACTIVE_GROUP = "submarine_active_group"
_LEGACY_VIEW = "claude_active_view"
_LEGACY_GROUP = "claude_active_group"


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
