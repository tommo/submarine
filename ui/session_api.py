"""Lazy session/registry lookups so ui/ loads before core/ exists.

All core/session imports are inside functions. Callers get None when the
kernel is not yet wired — listeners and chrome must tolerate that.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional


def _sublime():
    try:
        import sublime
        return sublime
    except Exception:
        return None


def sessions_map() -> dict:
    """view_id → Session derived from the live binding."""
    try:
        from core.registry import default_registry
        out = {}
        binding = getattr(default_registry, "binding", None)
        by_agent = getattr(default_registry, "by_agent", None)
        if isinstance(binding, dict) and isinstance(by_agent, dict):
            for vid, aid in binding.items():
                s = by_agent.get(aid)
                if s is not None:
                    out[vid] = s
            if out or by_agent:
                return out
    except Exception:
        pass
    sm = _sublime()
    if sm is None:
        return {}
    m = getattr(sm, "_submarine_sessions", None)
    if isinstance(m, dict):
        return m
    m = getattr(sm, "_claude_sessions", None)
    return m if isinstance(m, dict) else {}


def iter_sessions() -> Iterable[Any]:
    try:
        from core.registry import default_registry
        it = getattr(default_registry, "iter_sessions", None)
        if callable(it):
            found = list(it())
            if found:
                return found
        views = getattr(default_registry, "by_view", None)
        if isinstance(views, dict) and views:
            return list(views.values())
    except Exception:
        pass
    return list(sessions_map().values())


def get_session_for_view(view) -> Optional[Any]:
    if not view:
        return None
    try:
        vid = view.id()
    except Exception:
        return None
    try:
        from core.registry import default_registry
        fn = getattr(default_registry, "for_view", None)
        if callable(fn):
            hit = fn(view)
            if hit is not None:
                return hit
        fn = getattr(default_registry, "for_view_id", None) or getattr(
            default_registry, "get_session_for_view_id", None)
        if callable(fn):
            hit = fn(vid)
            if hit is not None:
                return hit
    except Exception:
        pass
    return sessions_map().get(vid)


def get_session_by_agent_id(agent_id: str) -> Optional[Any]:
    if not agent_id:
        return None
    try:
        from core.registry import default_registry
        fn = getattr(default_registry, "by_agent_id", None) or getattr(
            default_registry, "get_session_by_agent_id", None)
        if callable(fn):
            return fn(agent_id)
    except Exception:
        pass
    return None


def get_active_session(window) -> Optional[Any]:
    if not window:
        return None
    try:
        from core.registry import default_registry
        fn = getattr(default_registry, "active_for_window", None)
        if callable(fn):
            return fn(window)
    except Exception:
        pass
    try:
        view = window.active_view()
    except Exception:
        view = None
    if view:
        s = get_session_for_view(view)
        if s is not None:
            return s
    try:
        from .keys import ACTIVE_AGENT, ACTIVE_VIEW, read_setting
        aid = read_setting(window.settings(), ACTIVE_AGENT)
        if aid is not None:
            s = get_session_by_agent_id(aid)
            if s is not None:
                return s
        vid = read_setting(window.settings(), ACTIVE_VIEW)
        if vid is not None:
            return sessions_map().get(vid)
    except Exception:
        pass
    return None


def in_startup_quiet() -> bool:
    try:
        from core import in_startup_quiet as _fn
        return bool(_fn())
    except Exception:
        pass
    sm = _sublime()
    if sm is None:
        return False
    return bool(getattr(sm, "_submarine_startup_quiet", False)
                or getattr(sm, "_claude_startup_quiet", False))


def find_live_by_session_id(session_id: str):
    if not session_id:
        return None
    try:
        from core.registry import default_registry
        fn = getattr(default_registry, "find_live_by_session_id", None)
        if callable(fn):
            return fn(session_id)
    except Exception:
        pass
    for s in iter_sessions():
        if getattr(s, "session_id", None) == session_id:
            return s
    return None


def load_saved_sessions():
    try:
        from core.records import load_saved_sessions as _fn
        return _fn()
    except Exception:
        return []


def load_bookmarks(project_path=None):
    try:
        from core.records import load_bookmarks as _fn
        return _fn(project_path)
    except Exception:
        return set()


def remove_saved_session(session_id: str) -> bool:
    try:
        from core.records import remove_saved_session as _fn
        return bool(_fn(session_id))
    except Exception:
        return False


def rename_saved_session(session_id: str, name: str) -> bool:
    try:
        from core.records import rename_saved_session as _fn
        return bool(_fn(session_id, name))
    except Exception:
        return False


def toggle_bookmark(session_id: str, project_path=None, record=None) -> bool:
    try:
        from core.records import toggle_bookmark as _fn
        return bool(_fn(session_id, project_path, record=record))
    except Exception:
        return False


def save_bookmarks(starred, project_path=None, records=None) -> bool:
    try:
        from core.records import save_bookmarks as _fn
        _fn(starred, project_path, records=records)
        return True
    except Exception:
        return False


def load_bookmark_records(project_path=None):
    try:
        from core.records import load_bookmark_records as _fn
        return _fn(project_path)
    except Exception:
        return {}


def remember_active_session(window, view) -> None:
    try:
        from core.placement import remember_active_session as _fn
        _fn(window, view)
        return
    except Exception:
        pass
    if not window or not view:
        return
    try:
        from .keys import ACTIVE_AGENT, write_setting
        aid = None
        try:
            st = view.settings()
            aid = st.get("submarine_agent_id") or st.get("claude_agent_id")
        except Exception:
            aid = None
        if aid:
            write_setting(window.settings(), ACTIVE_AGENT, aid)
    except Exception:
        pass


def place_in_last_session_split(window, view) -> None:
    try:
        from core.placement import place_in_last_session_split as _fn
        _fn(window, view)
    except Exception:
        pass


def create_session(window, **kwargs):
    try:
        from core.session import create_session as _fn
        return _fn(window, **kwargs)
    except Exception:
        pass
    try:
        from core import create_session as _fn
        return _fn(window, **kwargs)
    except Exception:
        return None


def new_agent_id() -> str:
    try:
        from core.registry import new_agent_id as _fn
        return _fn()
    except Exception:
        import uuid
        return "submarine::" + uuid.uuid4().hex[:12]


def register_session(session) -> None:
    try:
        from core.registry import register_session as _fn
        _fn(session)
        return
    except Exception:
        pass
    view = None
    try:
        view = session.output.view if session.output else None
    except Exception:
        view = None
    if view is None:
        return
    sessions_map()[view.id()] = session


def unregister_view(view_id: int) -> None:
    try:
        from core.registry import unregister_view as _fn
        _fn(view_id)
        return
    except Exception:
        pass
    sessions_map().pop(view_id, None)


def keep_running_on_close(session) -> bool:
    try:
        from core.registry import keep_running_on_close as _fn
        return bool(_fn(session))
    except Exception:
        return bool(getattr(session, "keep_running_on_close", False))


def close_or_detach_session(session, view=None) -> None:
    try:
        from core.registry import close_or_detach_session as _fn
        _fn(session, view)
        return
    except Exception:
        pass
    try:
        from core.registry import detach_session as _fn
        _fn(session)
        return
    except Exception:
        pass
    try:
        if session is not None:
            session.stop()
    except Exception:
        pass


def detach_session(session) -> None:
    try:
        from core.registry import detach_session as _fn
        _fn(session)
    except Exception:
        pass


def sessions_for_window(window) -> list:
    try:
        from core.registry import sessions_for_window as _fn
        return list(_fn(window))
    except Exception:
        pass
    out = []
    for s in iter_sessions():
        if getattr(s, "window", None) is window:
            out.append(s)
    return out


def abbrev_for(backend: str) -> str:
    try:
        from backend.specs import abbrev_for as _fn
        return _fn(backend)
    except Exception:
        from plat.constants import BACKEND_ABBREV
        b = (backend or "claude").strip() or "claude"
        return BACKEND_ABBREV.get(b) or b[:2].upper()


def backend_theme(backend: str) -> str:
    try:
        from backend.specs import get as _get
        spec = _get(backend)
        theme = getattr(spec, "theme", None) or ""
        if theme:
            return theme
    except Exception:
        pass
    from .keys import THEME_CODEX, THEME_DEFAULT
    if backend == "codex":
        return THEME_CODEX
    return THEME_DEFAULT
