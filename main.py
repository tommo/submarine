"""Plugin lifecycle + Sublime-facing session factory.

Port of old core.py. core/session.create_session is the sublime-free kernel
constructor (ports in, Session out). This module is the ST host: it builds
ports, shows the sheet, registers, then start()s.

Package-level singletons (registry, session maps, devtools state) are rebound
onto the sublime module so they survive soft package reloads.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from typing import Any, Callable, Dict, Optional

# ST 3.8 loads this file as Submarine.main. Put the package root on sys.path
# so `import plat` / `import core` resolve the same way tests do, and alias
# this module as `main` so `from main import …` in commands/ is the same object.
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)
sys.modules.setdefault("main", sys.modules[__name__])

import sublime

from core.placement import place_in_last_session_split, remember_active_session
from core.registry import (
    default_registry,
    find_live_by_session_id,
    iter_sessions,
    register_session,
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
        "submit_with_modifier", "models", "ui_mode",
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


def _view_is_reconnecting(view):
    # type: (Any) -> bool
    if view is None:
        return False
    try:
        return bool(keys.read_setting(view.settings(), keys.RECONNECTING))
    except Exception:
        return False


def _is_restoring(window, attach_view=None):
    # type: (Any, Any) -> bool
    """True when create_session is attaching to a leftover sheet, not a new one.

    Listeners stamp RECONNECTING on the *orphan* view (often a background
    tab). Checking only the active view made background restores start()
    the bridge on Sublime restart.
    """
    if _view_is_reconnecting(attach_view):
        return True
    if window is None:
        return False
    views = []
    try:
        views = list(window.views() or [])
    except Exception:
        views = []
    if not views:
        try:
            active = window.active_view()
        except Exception:
            active = None
        if active is not None:
            views = [active]
    for view in views:
        if _view_is_reconnecting(view):
            return True
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
    if not hasattr(session, "show_queue_input"):
        def _show_queue_input(sess=session):
            if not sess.working:
                sess._enter_input_with_draft()
                return
            win = getattr(sess, "window", None)
            if win is None:
                return

            def on_done(text, s=sess):
                text = (text or "").strip()
                if text:
                    s.queue_prompt(text)

            win.show_input_panel(
                "Queue prompt:",
                sess.draft_prompt or "",
                on_done,
                None,
                None,
            )

        session.show_queue_input = _show_queue_input


def construct_session(
    window,
    resume_id=None,
    fork=False,
    profile=None,
    initial_context=None,
    backend=None,
    attach_view=None,
    model=None,
):
    # type: (Any, Optional[str], bool, Optional[dict], Optional[dict], Optional[str], Any, Optional[str]) -> Session
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
    try:
        from sidecar_skill import additional_skill_dirs
        for d in additional_skill_dirs():
            if d not in additional_dirs:
                additional_dirs.append(d)
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
        model=model,
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
    model=None,
):
    # type: (Any, Optional[str], bool, Optional[dict], Optional[str], Optional[dict], bool, Any, Optional[bool], Optional[bool], Optional[str]) -> Session
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
                from ui.host import HostView, is_single_mode
                if is_single_mode() and not getattr(existing, "torn_off", False):
                    HostView.for_window(window).attach(
                        window, existing, focus=focus)
                    return existing
            except Exception:
                pass
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

    restoring = _is_restoring(window, attach_view)
    if start is None:
        start = not restoring
    if show is None:
        show = not restoring

    old_session = None
    try:
        old_agent = keys.read_setting(window.settings(), keys.ACTIVE_AGENT)
        if old_agent:
            old_session = default_registry.by_agent_id(old_agent)
        if old_session is None:
            old_active = keys.read_setting(window.settings(), keys.ACTIVE_VIEW)
            if old_active:
                old_session = default_registry.for_view_id(old_active)
    except Exception:
        old_session = None
    if old_session is not None and not restoring:
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
        model=model,
    )
    session._composer_allowed = not restoring

    keys.write_setting(window.settings(), keys.CREATING_SESSION, True)
    try:
        attached = False
        if show and attach_view is None:
            try:
                from ui.host import HostView, is_single_mode
                if is_single_mode():
                    HostView.for_window(window).attach(
                        window, session, focus=focus)
                    attached = True
            except Exception:
                attached = False
        if show and not attached:
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
        register_session(session)
        if view is not None:
            try:
                session._persist_view_identity()
            except Exception:
                pass
            if not restoring:
                try:
                    remember_active_session(window, view)
                except Exception:
                    pass
            log_plugin(
                "create_session: agent_id=%s view_id=%s focus=%s start=%s"
                % (getattr(session, "agent_id", None), view.id(), focus, start)
            )
        else:
            log_plugin(
                "create_session: agent_id=%s viewless start=%s"
                % (getattr(session, "agent_id", None), start)
            )
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
        return default_registry.for_view(view)
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
    active_agent = keys.read_setting(window.settings(), keys.ACTIVE_AGENT)
    if active_agent:
        session = default_registry.by_agent_id(active_agent)
        if (
            session is not None
            and getattr(session, "window", None) == window
            and not getattr(session, "quick_mode", False)
        ):
            return session
    else:
        active_view_id = keys.read_setting(window.settings(), keys.ACTIVE_VIEW)
        if active_view_id:
            session = default_registry.for_view_id(active_view_id)
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
    sublime._submarine_by_agent = default_registry.by_agent  # type: ignore[attr-defined]
    sublime._submarine_binding = default_registry.binding  # type: ignore[attr-defined]
    sublime._submarine_sessions = default_registry.by_agent  # type: ignore[attr-defined]
    sublime._submarine_agents = default_registry.by_agent  # type: ignore[attr-defined]


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
    seen = set()

    def _drop_session(s):
        if s is None or id(s) in seen:
            return
        if not hasattr(s, "output") and not hasattr(s, "client"):
            return
        seen.add(id(s))
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

    held = getattr(sublime, "_submarine_registry", None)
    if held is not None:
        try:
            live = list(held.iter_sessions())
            if live:
                log_plugin("plugin_loaded: dropping %d stale session(s)" % len(live))
            for s in live:
                _drop_session(s)
            if held is not default_registry:
                held.clear()
        except Exception:
            pass

    if default_registry.by_agent:
        log_plugin(
            "plugin_loaded: clearing %d live registry session(s)"
            % len(default_registry.by_agent)
        )
        for s in list(default_registry.iter_sessions()):
            _drop_session(s)
        default_registry.clear()

    for attr in (
        "_submarine_sessions", "_claude_sessions",
        "_submarine_by_agent", "_submarine_background", "_claude_background",
        "_submarine_agents", "_claude_agents",
    ):
        prev = getattr(sublime, attr, None)
        if isinstance(prev, dict) and prev:
            for s in list(prev.values()):
                _drop_session(s)
            try:
                prev.clear()
            except Exception:
                pass


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
        # Restoring reopens one sheet per previously-open session. In single
        # mode only the host may stay, so collapse the duplicates before the
        # user creates anything — otherwise a new session lands on a second
        # "host" sheet.
        try:
            from ui.host import settle_single_view
            for w in sublime.windows():
                try:
                    n = settle_single_view(w)
                    if n:
                        log_plugin(
                            "startup settle: collapsed %d output sheet(s)" % n)
                except Exception as e:
                    log_plugin("startup settle (window): %s" % e)
        except Exception as e:
            log_plugin("startup settle (single view): %s" % e)
        try:
            from ui.listeners import restore_current_sessions_from_store
            n = restore_current_sessions_from_store()
            if n:
                log_plugin("startup settle: restored %d current session(s)" % n)
        except Exception as e:
            log_plugin("startup settle (current list): %s" % e)
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


def _run_legacy_migration():
    """One-time import from a sibling sublime-claude / ClaudeCode install."""
    try:
        from core.migrate import run_migration
        user_packages_dir = os.path.join(sublime.packages_path(), "User")
        override = None
        try:
            override = sublime.load_settings(SETTINGS_FILE).get("legacy_claude_dir") or None
        except Exception:
            override = None
        report = run_migration(PLUGIN_DIR, user_packages_dir, override_dir=override)
        log_plugin("migration: %s" % report)
    except Exception as e:
        log_plugin("migration: %s" % e)


def plugin_loaded():
    global _PLUGIN_LOADED_AT
    _PLUGIN_LOADED_AT = time.time()
    try:
        sublime._submarine_startup_quiet = True  # type: ignore[attr-defined]
    except Exception:
        pass

    _drop_stale_sessions()
    _reap_orphan_bridges()
    _bind_registry()
    _install_create_session_compat()
    _run_legacy_migration()

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
        from features.webui.hosted import start as web_start
        web_start()
    except Exception as e:
        log_plugin("web ui start failed: %s" % e)

    try:
        from features.devtools.server import start as devtools_start
        devtools_start()
    except Exception as e:
        log_plugin("devtools start failed: %s" % e)

    schedule_auto_sleep()
    try:
        from ui.host import ui_mode
        sublime._submarine_ui_mode = ui_mode()  # type: ignore[attr-defined]
        sublime.load_settings(SETTINGS_FILE).add_on_change(
            "submarine_ui_mode", _on_ui_mode_change)
    except Exception as e:
        log_plugin("ui_mode watch: %s" % e)
    try:
        from ui.sheet import apply_output_settings_to_all_output_views
        sublime.load_settings(keys.OUTPUT_SETTINGS).add_on_change(
            "submarine_output_all", apply_output_settings_to_all_output_views)
        apply_output_settings_to_all_output_views()
    except Exception as e:
        log_plugin("output settings watch: %s" % e)
    sublime.set_timeout(_startup_strip_composers, 0)
    sublime.set_timeout(_startup_strip_composers, 100)
    # Quiet covers settle so on_activated cannot restore+enter ◎ in the
    # gap between the timer and the one-shot park-asleep pass.
    sublime.set_timeout(_startup_settle_views, int(_STARTUP_QUIET_S * 1000) + 50)
    sublime.set_timeout(_end_startup_quiet, int(_STARTUP_QUIET_S * 1000) + 150)


def _end_startup_quiet():
    try:
        sublime._submarine_startup_quiet = False  # type: ignore[attr-defined]
    except Exception:
        pass


def _on_ui_mode_change():
    try:
        from ui.host import apply_ui_mode, ui_mode
        mode = ui_mode()
        prev = getattr(sublime, "_submarine_ui_mode", None)
        if prev == mode:
            return
        sublime._submarine_ui_mode = mode  # type: ignore[attr-defined]
        if prev is None:
            return
        for w in sublime.windows():
            apply_ui_mode(w, mode)
    except Exception as e:
        log_plugin("ui_mode change: %s" % e)


def _shutdown_live_bridges():
    # type: () -> int
    """Ask every live session's bridge to exit before the modules unload.

    A soft reload replaces the plugin's Python while the bridge subprocesses
    keep running — the host process still holds their pipes, so they never see
    EOF and nothing shuts them down.
    """
    n = 0
    try:
        live = list(iter_sessions())
    except Exception:
        live = list((getattr(sublime, "_submarine_sessions", None) or {}).values())
    for s in live:
        client = getattr(s, "client", None)
        if client is None:
            continue
        try:
            client.send("shutdown", {})
            n += 1
        except Exception:
            pass
    return n


def _orphan_bridge_pids(ps_text, self_pid):
    # type: (str, int) -> list
    """PIDs of this host's leftover bridge processes, deepest first.

    Descendants of this plugin_host only, so another Sublime instance running
    the same plugin dir is never touched. Codex is the backend where a leak is
    not merely wasted memory: an abandoned app-server keeps the thread writer,
    and the next resume of that thread fails with -32600 "already has an active
    writer". Bridges are matched too — killing the codex child first lets the
    bridge see EOF instead of respawning it.
    """
    rows = {}  # type: dict
    for line in (ps_text or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        rows[pid] = (ppid, parts[2])

    def _ours(pid):
        depth = 0
        seen = set()
        while pid in rows and pid not in seen:
            seen.add(pid)
            ppid = rows[pid][0]
            depth += 1
            if ppid == self_pid:
                return depth
            pid = ppid
        return 0

    found = []  # type: list
    for pid, (_ppid, cmd) in rows.items():
        ours = _ours(pid)
        if not ours:
            continue
        if "app-server" in cmd and "--agent-id=agent-" in cmd:
            found.append((ours, pid))
        elif "/bridge/" in cmd and "_main.py" in cmd:
            found.append((ours, pid))
    # Deepest first: the codex child dies before its bridge can respawn it.
    found.sort(reverse=True)
    return [pid for _depth, pid in found]


def _reap_orphan_bridges():
    # type: () -> int
    """Kill bridge children left behind by an earlier plugin load."""
    try:
        import subprocess
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,command="],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5,
        ).stdout.decode("utf-8", "replace")
    except Exception:
        return 0
    pids = _orphan_bridge_pids(out, os.getpid())
    if not pids:
        return 0
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except OSError:
            pass
    # Escalate for anything that ignored SIGTERM.
    try:
        time.sleep(0.3)
        for pid in pids:
            try:
                os.kill(pid, 0)
            except OSError:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    except Exception:
        pass
    if killed:
        log_plugin("plugin_loaded: reaped %d orphan bridge process(es): %s"
                   % (killed, pids))
    return killed


def plugin_unloaded():
    try:
        n = _shutdown_live_bridges()
        if n:
            log_plugin("plugin_unloaded: asked %d bridge(s) to stop" % n)
    except Exception as e:
        log_plugin("plugin_unloaded bridges: %s" % e)
    try:
        n = 0
        for s in list(iter_sessions()):
            if getattr(s, "quick_mode", False):
                continue
            try:
                s._save_session()
            except Exception:
                pass
            try:
                s._persist_state("sleeping")
            except Exception:
                pass
            n += 1
        if n:
            log_plugin("plugin_unloaded: saved %d current session(s)" % n)
    except Exception as e:
        log_plugin("plugin_unloaded save: %s" % e)
    try:
        sublime.load_settings(SETTINGS_FILE).clear_on_change("submarine_ui_mode")
    except Exception:
        pass
    try:
        for w in sublime.windows():
            for v in w.views():
                if keys.is_output_view(v):
                    _clear_view_phantoms(v)
    except Exception:
        pass

    try:
        from ui.session_list import stop_session_list_poll
        stop_session_list_poll()
    except Exception:
        pass

    try:
        from features.devtools.server import stop as devtools_stop
        devtools_stop()
    except Exception:
        pass

    try:
        from features.webui.hosted import stop as web_stop
        web_stop()
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
