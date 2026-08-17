"""Plugin lifecycle + Sublime-facing session factory.

Port of old core.py. core/session.create_session is the sublime-free kernel
constructor (ports in, Session out). This module is the ST host: it builds
ports, shows the sheet, registers, then start()s.

Package-level singletons (registry, session maps, devtools state) are rebound
onto the sublime module so they survive soft package reloads.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, Optional

import sublime

from core.placement import place_in_last_session_split, remember_active_session
from core.registry import (
    default_registry,
    find_live_by_session_id,
    iter_sessions,
    register_session,
    relink_all_parents,
    unregister_view,
)
from core.session import Session
from plat.constants import DEFAULT_SESSION_NAME, SETTINGS_FILE
from ui import keys
from ui.view import SubmarineOutputView

try:
    from plat.log import log_plugin
except Exception:  # pragma: no cover
    def log_plugin(message):  # type: ignore
        print("[Submarine] %s" % message)


PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

_auto_sleep_timer = None  # type: Optional[Any]
_PLUGIN_LOADED_AT = 0.0
_STARTUP_QUIET_S = 3.0
_kernel_create_session = None  # type: Optional[Callable]


class SublimeScheduler(object):
    """Scheduler port: sublime.set_timeout. Tokens cannot be cancelled."""

    def call_later(self, ms, fn):
        # type: (int, Callable[[], None]) -> Any
        return sublime.set_timeout(fn, int(ms))


class ViewPersist(object):
    """PersistPort over view.settings(). View may be assigned after construct."""

    def __init__(self, get_view):
        # type: (Callable[[], Any]) -> None
        self._get_view = get_view

    def _settings(self):
        view = None
        try:
            view = self._get_view()
        except Exception:
            view = None
        if view is None or not getattr(view, "is_valid", lambda: False)():
            return None
        try:
            return view.settings()
        except Exception:
            return None

    def stamp(self, key, value):
        st = self._settings()
        if st is not None:
            keys.write_setting(st, key, value)

    def read(self, key):
        st = self._settings()
        if st is None:
            return None
        return keys.read_setting(st, key)

    def clear(self, key):
        st = self._settings()
        if st is not None:
            keys.erase_setting(st, key)


def in_startup_quiet():
    # type: () -> bool
    """True for a few seconds after plugin load / package reload."""
    if _PLUGIN_LOADED_AT <= 0:
        return False
    return (time.time() - _PLUGIN_LOADED_AT) < _STARTUP_QUIET_S


def _plugin_settings_dict():
    # type: () -> Dict[str, Any]
    try:
        s = sublime.load_settings(SETTINGS_FILE)
    except Exception:
        return {}
    out = {}  # type: Dict[str, Any]
    for key in (
        "python_path", "default_model", "default_backend", "default_models",
        "allowed_tools", "permission_mode", "auto_sleep_minutes",
        "quota_service_url", "effort", "goal_skeptic_mode",
        "mcp_enable_read_image", "auto_retry_turns", "auto_retry_backoff_seconds",
        "custom_providers", "deepseek_api_key", "quick_agent", "env",
        "submit_with_modifier", "models",
    ):
        try:
            val = s.get(key)
        except Exception:
            val = None
        if val is not None:
            out[key] = val
    return out


def _default_backend():
    # type: () -> str
    try:
        s = sublime.load_settings(SETTINGS_FILE)
        return s.get("default_backend", "claude") or "claude"
    except Exception:
        return "claude"


def _is_restoring(window):
    # type: (Any) -> bool
    """Listeners set RECONNECTING before calling create_session to attach."""
    if window is None:
        return False
    try:
        view = window.active_view()
    except Exception:
        view = None
    if view is None:
        return False
    try:
        return bool(keys.read_setting(view.settings(), keys.RECONNECTING))
    except Exception:
        return False


def _attach_session_shims(session):
    # type: (Session) -> None
    """Fill Session hooks that features/core leave for the host."""
    ctx = getattr(session, "context", None)
    if ctx is not None:
        if not hasattr(session, "add_context_file"):
            session.add_context_file = ctx.add_file
        if not hasattr(session, "add_context_selection"):
            session.add_context_selection = ctx.add_selection
        if not hasattr(session, "add_context_folder"):
            session.add_context_folder = ctx.add_folder
        if not hasattr(session, "add_context_path"):
            session.add_context_path = ctx.add_path
        if not hasattr(session, "add_context_image"):
            session.add_context_image = ctx.add_image
        if not hasattr(session, "clear_context"):
            session.clear_context = ctx.clear
        if not hasattr(session, "pending_context"):
            session.pending_context = ctx.items
    if not hasattr(session, "_enter_input_with_draft"):
        session._enter_input_with_draft = session._enter_input_if_idle


def construct_session(
    window,
    resume_id=None,
    fork=False,
    profile=None,
    initial_context=None,
    backend=None,
    attach_view=None,
):
    # type: (Any, Optional[str], bool, Optional[dict], Optional[dict], Optional[str], Any) -> Session
    """Build a Session with ST ports. Does not show, register, or start."""
    if backend is None:
        backend = _default_backend()
    output = SubmarineOutputView(window)
    if attach_view is not None:
        output.view = attach_view
    persist = ViewPersist(lambda o=output: o.view)
    scheduler = SublimeScheduler()
    cwd = ""
    try:
        folders = window.folders() if window else None
        if folders:
            cwd = folders[0]
    except Exception:
        cwd = ""
    additional_dirs = []
    try:
        s = sublime.load_settings(SETTINGS_FILE)
        extra = s.get("submarine_additional_dirs") or s.get("claude_additional_dirs") or []
        if isinstance(extra, list):
            additional_dirs = [str(x) for x in extra if x]
    except Exception:
        pass
    session = Session(
        output=output,
        chrome=output,
        scheduler=scheduler,
        persist=persist,
        registry=default_registry,
        resume_id=resume_id,
        fork=fork,
        profile=profile,
        initial_context=initial_context,
        backend=backend,
        window=window,
        cwd=cwd,
        additional_dirs=additional_dirs,
        settings=_plugin_settings_dict(),
        plugin_dir=PLUGIN_DIR,
    )
    try:
        from features import wire_session
        wire_session(session)
    except Exception as e:
        log_plugin("wire_session: %s" % e)
    _attach_session_shims(session)
    return session


def create_session(
    window,
    resume_id=None,
    fork=False,
    profile=None,
    initial_context=None,
    backend=None,
    focus=True,
    attach_view=None,
    start=None,
    show=None,
):
    # type: (Any, Optional[str], bool, Optional[dict], Optional[str], Optional[dict], bool, Any, Optional[bool], Optional[bool]) -> Session
    """Sublime-facing factory: live-resume dedupe, guard, register, start.

    Invariants:
    - One live Session per session_id (resume, not fork) → reveal existing.
    - ``submarine_creating_session`` is set around show() so on_activated
      restore does not attach a sleeping session to a brand-new sheet.
    - Register before start() so later activation finds this session.
    """
    if resume_id and not fork:
        existing = find_live_by_session_id(resume_id)
        if existing is not None:
            try:
                from ui.session_list import reveal_live_session
                if reveal_live_session(window, existing, focus=focus):
                    return existing
            except Exception:
                try:
                    if existing.output:
                        existing.output.show(focus=focus)
                except Exception:
                    pass
                return existing

    if backend is None:
        backend = _default_backend()

    restoring = attach_view is None and _is_restoring(window)
    if start is None:
        start = not restoring
    if show is None:
        show = not restoring

    old_active = None
    try:
        old_active = keys.read_setting(window.settings(), keys.ACTIVE_VIEW)
    except Exception:
        old_active = None
    if old_active and not restoring:
        old_session = default_registry.get_session_for_view_id(old_active)
        if old_session is not None:
            try:
                old_session.output.set_name(
                    getattr(old_session, "display_name", None)
                    or old_session.name
                    or DEFAULT_SESSION_NAME
                )
            except Exception:
                pass

    session = construct_session(
        window,
        resume_id=resume_id,
        fork=fork,
        profile=profile,
        initial_context=initial_context,
        backend=backend,
        attach_view=attach_view,
    )
    session._composer_allowed = True

    keys.write_setting(window.settings(), keys.CREATING_SESSION, True)
    try:
        if show:
            session.output.show(focus=focus)
            view = session.output.view
            if resume_id and view is not None:
                try:
                    if place_in_last_session_split(window, view) and focus:
                        window.focus_view(view)
                except Exception:
                    pass
        view = session.output.view
        if view is not None and backend != "claude":
            try:
                from backend.specs import get as spec_get
                spec = spec_get(backend)
                keys.write_setting(view.settings(), keys.BACKEND, backend)
                session.output.set_name(spec.label or backend)
                if spec.theme:
                    view.settings().set("color_scheme", spec.theme)
            except Exception:
                keys.write_setting(view.settings(), keys.BACKEND, backend)
        if view is not None:
            try:
                session._persist_view_identity()
            except Exception:
                pass
            register_session(session)
            try:
                remember_active_session(window, view)
            except Exception:
                pass
            log_plugin(
                "create_session: agent_id=%s view_id=%s focus=%s start=%s"
                % (getattr(session, "agent_id", None), view.id(), focus, start)
            )
        else:
            log_plugin("create_session: ERROR - no output view")
        if start:
            session.start()
    finally:
        keys.erase_setting(window.settings(), keys.CREATING_SESSION)

    schedule_auto_sleep()
    try:
        from ui.session_list import schedule_session_list_refresh
        schedule_session_list_refresh()
    except Exception:
        pass
    return session


def get_session_for_view(view):
    # type: (Any) -> Optional[Session]
    if view is None:
        return None
    try:
        return default_registry.get_session_for_view_id(view.id())
    except Exception:
        return None


def get_active_session(window):
    # type: (Any) -> Optional[Session]
    """Active output view, else last-active, else a live session in the window."""
    if window is None:
        return None
    view = None
    try:
        view = window.active_view()
    except Exception:
        view = None
    if view is not None and keys.is_output_view(view):
        s = get_session_for_view(view)
        if s is not None:
            return s
    working = None
    for session in default_registry.sessions_for_window(window):
        if session.working:
            working = session
            if not getattr(session, "quick_mode", False):
                return session
    if working is not None:
        return working
    active_view_id = keys.read_setting(window.settings(), keys.ACTIVE_VIEW)
    if active_view_id:
        session = default_registry.get_session_for_view_id(active_view_id)
        if (
            session is not None
            and getattr(session, "window", None) == window
            and not getattr(session, "quick_mode", False)
        ):
            return session
    for session in default_registry.sessions_for_window(window):
        if not getattr(session, "quick_mode", False):
            return session
    try:
        from features.quick import get_quick_session
        qs = get_quick_session(window)
        if qs is not None:
            return qs
    except Exception:
        pass
    return None


def _bind_registry():
    """Keep the process-level registry on sublime so soft reloads see it."""
    sublime._submarine_registry = default_registry  # type: ignore[attr-defined]
    sublime._submarine_sessions = default_registry.sessions  # type: ignore[attr-defined]
    sublime._submarine_agents = default_registry.agents  # type: ignore[attr-defined]
    sublime._submarine_background = default_registry.background  # type: ignore[attr-defined]


def _abort_session_ui(s):
    """Clear ⚙ rows before dropping a Session on reload. Do not SIGTERM."""
    try:
        bg = getattr(s, "bg", None)
        if bg is not None:
            bg.abort()
    except Exception:
        pass
    try:
        output = getattr(s, "output", None)
        if output is not None and hasattr(output, "reset_active_states"):
            output.reset_active_states()
    except Exception:
        pass


def _drop_stale_sessions():
    """Drop Python refs without terminating live bridges (reload invariant)."""
    prev = getattr(sublime, "_submarine_sessions", None)
    if not isinstance(prev, dict):
        prev = getattr(sublime, "_claude_sessions", None)
    if isinstance(prev, dict) and prev:
        log_plugin("plugin_loaded: dropping %d stale session(s)" % len(prev))
        for s in list(prev.values()):
            _abort_session_ui(s)
            try:
                if getattr(s, "client", None):
                    s.client = None
            except Exception:
                pass
            try:
                v = s.output.view if getattr(s, "output", None) else None
                if v is not None and v.is_valid():
                    _clear_view_phantoms(v)
            except Exception:
                pass
        prev.clear()

    held = getattr(sublime, "_submarine_registry", None)
    if held is not None and held is not default_registry:
        try:
            for s in list(held.iter_sessions()):
                _abort_session_ui(s)
                try:
                    if getattr(s, "client", None):
                        s.client = None
                except Exception:
                    pass
            held.clear()
        except Exception:
            pass

    if default_registry.sessions:
        log_plugin(
            "plugin_loaded: clearing %d live registry session(s)"
            % len(default_registry.sessions)
        )
        for s in list(default_registry.iter_sessions()):
            _abort_session_ui(s)
            try:
                if getattr(s, "client", None):
                    s.client = None
            except Exception:
                pass
        default_registry.clear()

    bg = getattr(sublime, "_submarine_background", None)
    if not isinstance(bg, dict):
        bg = getattr(sublime, "_claude_background", None)
    if isinstance(bg, dict) and bg:
        log_plugin("plugin_loaded: dropping %d background session(s)" % len(bg))
        for s in list(bg.values()):
            _abort_session_ui(s)
            try:
                if getattr(s, "client", None):
                    s.client = None
            except Exception:
                pass
        bg.clear()


def _clear_view_phantoms(view):
    for name in (
        keys.PHANTOM_SLEEP, keys.PHANTOM_QUEUE, keys.PHANTOM_WAKEUP,
        keys.PHANTOM_PERM_BANNER, keys.PHANTOM_PAD, keys.PHANTOM_MEDIA,
        keys.PHANTOM_CONTEXT, keys.PHANTOM_TURN_CONTEXT,
        "claude_sleep", "claude_queue", "claude_wakeup",
        "claude_permission_banner", "claude_composer_pad",
    ):
        try:
            view.erase_phantoms(name)
        except Exception:
            pass


def _startup_strip_composers():
    try:
        n = 0
        for w in sublime.windows():
            for v in w.views():
                if not keys.is_output_view(v):
                    continue
                if keys.read_setting(v.settings(), keys.QUICK):
                    continue
                keys.write_setting(v.settings(), keys.INPUT_MODE, False)
                _clear_view_phantoms(v)
                if SubmarineOutputView.strip_composer_tail(v):
                    n += 1
        if n:
            log_plugin("startup strip: removed leftover ◎ on %d view(s)" % n)
    except Exception:
        pass


def _startup_settle_views():
    try:
        _startup_strip_composers()
        from ui.listeners import settle_startup_output_views
        settle_startup_output_views()
        n = relink_all_parents()
        if n:
            log_plugin("startup settle: relinked %d parent view_id(s)" % n)
    except Exception as e:
        log_plugin("startup settle: %s" % e)


def _install_create_session_compat():
    """ui.session_api.create_session imports core.session.create_session at
    call time and passes a window. Point that at this factory when the first
    arg looks like a Window. Kernel tests keep the port-based constructor.
    """
    global _kernel_create_session
    import core.session as cs
    if _kernel_create_session is None:
        _kernel_create_session = cs.create_session

    kernel = _kernel_create_session

    def _compat(*args, **kwargs):
        if args and hasattr(args[0], "new_file"):
            return create_session(*args, **kwargs)
        return kernel(*args, **kwargs)

    cs.create_session = _compat
    try:
        import core as core_pkg
        core_pkg.create_session = _compat
    except Exception:
        pass


def plugin_loaded():
    global _PLUGIN_LOADED_AT
    _PLUGIN_LOADED_AT = time.time()
    try:
        sublime._submarine_startup_quiet = True  # type: ignore[attr-defined]
    except Exception:
        pass

    _drop_stale_sessions()
    _bind_registry()
    _install_create_session_compat()

    try:
        from features.quick import set_session_factory
        set_session_factory(create_session)
    except Exception as e:
        log_plugin("quick factory: %s" % e)

    try:
        n = 0
        for w in sublime.windows():
            for v in w.views():
                if keys.is_output_view(v):
                    _clear_view_phantoms(v)
                    n += 1
        if n:
            log_plugin("plugin_loaded: cleared phantoms on %d output view(s)" % n)
    except Exception as e:
        log_plugin("plugin_loaded: phantom clear: %s" % e)

    try:
        from mcp.socket_server import start as mcp_start
        mcp_start()
    except Exception as e:
        log_plugin("MCP socket start failed: %s" % e)

    try:
        from features.devtools.server import start as devtools_start
        devtools_start()
    except Exception as e:
        log_plugin("devtools start failed: %s" % e)

    schedule_auto_sleep()
    sublime.set_timeout(_startup_strip_composers, 0)
    sublime.set_timeout(_startup_strip_composers, 100)
    sublime.set_timeout(_startup_settle_views, int(_STARTUP_QUIET_S * 1000) + 50)
    sublime.set_timeout(_end_startup_quiet, int(_STARTUP_QUIET_S * 1000))


def _end_startup_quiet():
    try:
        sublime._submarine_startup_quiet = False  # type: ignore[attr-defined]
    except Exception:
        pass


def plugin_unloaded():
    try:
        for w in sublime.windows():
            for v in w.views():
                if keys.is_output_view(v):
                    _clear_view_phantoms(v)
    except Exception:
        pass

    try:
        from features.devtools.server import stop as devtools_stop
        devtools_stop()
    except Exception:
        pass

    try:
        from mcp.socket_server import stop as mcp_stop
        mcp_stop()
    except Exception:
        pass


def _check_auto_sleep():
    global _auto_sleep_timer
    _auto_sleep_timer = None

    settings = sublime.load_settings(SETTINGS_FILE)
    timeout_min = settings.get("auto_sleep_minutes", 60)
    if not timeout_min or timeout_min <= 0:
        return

    now = time.time()
    live = []
    try:
        live = iter_sessions()
    except Exception:
        live = list((getattr(sublime, "_submarine_sessions", None) or {}).values())

    for session in live:
        try:
            gt = getattr(session, "goal_tracker", None)
            if gt is not None and gt.is_open() and gt.status in (
                    "active", "infra_paused"):
                continue
        except Exception:
            pass
        force = session.should_auto_sleep(now, timeout_min)
        if force is None:
            continue
        idle_m = int((now - session.effective_idle_at()) / 60)
        log_plugin(
            "auto-sleep: %s idle ~%sm (timeout=%sm force=%s)"
            % (session.name, idle_m, timeout_min, bool(force))
        )
        session.sleep(force=bool(force))

    schedule_auto_sleep()


def schedule_auto_sleep():
    global _auto_sleep_timer
    if _auto_sleep_timer is not None:
        return
    try:
        if not iter_sessions():
            return
    except Exception:
        if not getattr(sublime, "_submarine_sessions", None):
            return
    _auto_sleep_timer = sublime.set_timeout(_check_auto_sleep, 60000)
