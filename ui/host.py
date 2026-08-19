"""One output view per window (ui_mode=single). Session list swaps the bound session.

Tabs mode never enters this module's attach/detach path from default
callers — every branch is gated on `is_single_mode()`. Detaching never
stops, interrupts, or sleeps a bridge.
"""
from __future__ import annotations

from typing import Any, Optional

from plat.constants import SETTINGS_FILE

from . import keys
from .sheet import OutputSheet

try:
    import sublime
except ImportError:
    sublime = None  # type: ignore


UI_MODE_TABS = "tabs"
UI_MODE_SINGLE = "single"
PLACEHOLDER = "(no session)\n"

_INSTANCES = {}  # type: dict
_mode_override = None  # type: Optional[str]


def set_ui_mode_override(mode):
    # type: (Optional[str]) -> None
    """Test hook. Production never calls this."""
    global _mode_override
    _mode_override = mode


def ui_mode():
    # type: () -> str
    if _mode_override is not None:
        raw = _mode_override
    elif sublime is None:
        return UI_MODE_TABS
    else:
        try:
            raw = sublime.load_settings(SETTINGS_FILE).get("ui_mode", UI_MODE_TABS)
        except Exception:
            return UI_MODE_TABS
    mode = (raw or UI_MODE_TABS)
    if isinstance(mode, str):
        mode = mode.strip().lower()
    else:
        mode = UI_MODE_TABS
    if mode == UI_MODE_SINGLE:
        return UI_MODE_SINGLE
    return UI_MODE_TABS


def is_single_mode():
    # type: () -> bool
    return ui_mode() == UI_MODE_SINGLE


def _window_key(window):
    # type: (Any) -> Any
    if window is None:
        return None
    try:
        return window.id()
    except Exception:
        return id(window)


class _DummyOwner(object):
    """Minimal OutputSheet owner used only to create the host view."""

    def __init__(self):
        self.conversations = []
        self.current = None

    def repaint_from_state(self):
        pass


class HostView(object):
    """One instance per window. Lazy. Owns the host ST view in single mode."""

    def __init__(self, window):
        self.window = window
        self._view = None  # type: Any

    @classmethod
    def for_window(cls, window):
        # type: (Any) -> HostView
        key = _window_key(window)
        hv = _INSTANCES.get(key)
        if hv is None:
            hv = cls(window)
            if key is not None:
                _INSTANCES[key] = hv
        else:
            hv.window = window
        return hv

    @classmethod
    def reset(cls):
        # type: () -> None
        global _mode_override
        _INSTANCES.clear()
        _mode_override = None

    @classmethod
    def forget_view_id(cls, view_id):
        # type: (Any) -> None
        if view_id is None:
            return
        try:
            vid = int(view_id)
        except (TypeError, ValueError):
            return
        for hv in list(_INSTANCES.values()):
            view = hv._view
            if view is None:
                continue
            try:
                if view.id() == vid:
                    hv._view = None
            except Exception:
                continue

    def forget_view(self, view=None):
        # type: (Any) -> None
        if view is not None and self._view is not None:
            try:
                if self._view.id() != view.id():
                    return
            except Exception:
                pass
        self._view = None

    def host_view(self, window=None, create_via=None):
        # type: (Any, Any) -> Any
        """The window's single output ST view. Created via OutputSheet.show()."""
        window = window or self.window
        if self._view is not None:
            try:
                if self._view.is_valid():
                    return self._view
            except Exception:
                pass
            self._view = None
        found = self._find_existing(window)
        if found is not None:
            self._view = found
            self._stamp_host(found)
            return found
        via_view = getattr(create_via, "view", None)
        if via_view is not None:
            try:
                if via_view.is_valid():
                    self._stamp_host(via_view)
                    self._view = via_view
                    return via_view
            except Exception:
                pass
        if create_via is not None and getattr(create_via, "sheet", None) is not None:
            sheet = create_via.sheet
            sheet.window = window
            sheet.show(focus=False, create=True)
            v = sheet.view
        else:
            dummy = _DummyOwner()
            sheet = OutputSheet(dummy, window)
            sheet.show(focus=False, create=True)
            v = sheet.view
        if v is None:
            return None
        self._stamp_host(v)
        self._view = v
        return v

    def _find_existing(self, window):
        # type: (Any) -> Any
        if not window:
            return None
        try:
            views = list(window.views())
        except Exception:
            return None
        for v in views:
            try:
                if not v.is_valid():
                    continue
                if not keys.is_output_view(v):
                    continue
                if keys.read_setting(v.settings(), keys.QUICK):
                    continue
                if keys.read_setting(v.settings(), keys.HOST, False):
                    return v
            except Exception:
                continue
        return None

    def _stamp_host(self, view):
        # type: (Any) -> None
        try:
            st = view.settings()
            keys.write_setting(st, keys.HOST, True)
            keys.write_setting(st, keys.OUTPUT, True)
        except Exception:
            pass

    def attach(self, window, session, focus=True):
        # type: (Any, Any, bool) -> bool
        """Bind `session` to the host view. Never stops the outgoing bridge."""
        if not session or not window:
            return False
        flagged = False
        try:
            already = bool(keys.read_setting(window.settings(), keys.CREATING_SESSION))
            if not already:
                keys.write_setting(window.settings(), keys.CREATING_SESSION, True)
                flagged = True
        except Exception:
            flagged = False
        try:
            return self._attach(window, session, focus=focus)
        finally:
            if flagged:
                try:
                    keys.erase_setting(window.settings(), keys.CREATING_SESSION)
                except Exception:
                    pass

    def _attach(self, window, session, focus=True):
        # type: (Any, Any, bool) -> bool
        from core.placement import remember_active_session
        from core.registry import bind, default_registry, for_view

        host = self.host_view(window, create_via=getattr(session, "output", None))
        if host is None:
            return False

        current = for_view(host)
        if current is not None and current is session:
            self._finish_bound(window, session, host, focus=focus)
            return True

        if current is not None:
            self._detach_session(current)

        default_registry.register(session)
        aid = getattr(session, "agent_id", None)
        if aid:
            bind(aid, host.id())
        session.window = window
        output = getattr(session, "output", None)
        if output is not None:
            try:
                output.window = window
            except Exception:
                pass
            output.view = host

        self._paint_bound(session, host)
        self._finish_bound(window, session, host, focus=focus)
        try:
            remember_active_session(window, host)
        except Exception:
            pass
        try:
            from ui.session_list import schedule_session_list_refresh
            schedule_session_list_refresh()
        except Exception:
            pass
        return True

    def _detach_session(self, session):
        # type: (Any) -> None
        from core.registry import unbind

        output = getattr(session, "output", None)
        if output is not None:
            save = getattr(output, "surface_save", None)
            if callable(save):
                try:
                    session.surface = save()
                except Exception:
                    pass
        aid = getattr(session, "agent_id", None)
        if aid:
            unbind(aid)
        if output is not None:
            try:
                output.view = None
            except Exception:
                pass

    def _paint_bound(self, session, host):
        # type: (Any, Any) -> None
        output = getattr(session, "output", None)
        if output is None:
            return
        try:
            output.repaint_from_state()
        except Exception:
            pass
        try:
            persist = getattr(session, "_persist_view_identity", None)
            if callable(persist):
                persist()
        except Exception:
            pass
        try:
            backend = getattr(session, "backend", None) or "claude"
            keys.write_setting(host.settings(), keys.BACKEND, backend)
            sheet = getattr(output, "sheet", None)
            if sheet is not None and hasattr(sheet, "apply_theme"):
                sheet.apply_theme(backend)
        except Exception:
            pass
        try:
            name = getattr(session, "display_name", None) or getattr(session, "name", None)
            if name:
                output.set_name(name)
        except Exception:
            pass
        try:
            modals = getattr(output, "modals", None)
            if modals is not None and hasattr(modals, "rerender_pending"):
                modals.rerender_pending()
        except Exception:
            pass
        snap = getattr(session, "surface", None) or {}
        if not snap:
            snap = getattr(output, "_surface", None) or {}
        restore = getattr(output, "surface_restore", None)
        if callable(restore):
            try:
                restore(snap)
            except Exception:
                pass

    def _finish_bound(self, window, session, host, focus=True):
        # type: (Any, Any, Any, bool) -> None
        setter = getattr(session, "_set_unread", None)
        if callable(setter):
            try:
                setter(False)
            except Exception:
                pass
        else:
            session.unread = False
            chrome = getattr(session, "chrome", None) or getattr(session, "output", None)
            if chrome is not None and hasattr(chrome, "set_unread"):
                try:
                    chrome.set_unread(False)
                except Exception:
                    pass
        try:
            output = getattr(session, "output", None)
            if output is not None and hasattr(output, "refresh_tab_title"):
                output.refresh_tab_title()
        except Exception:
            pass
        if focus:
            try:
                window.focus_view(host)
            except Exception:
                pass

    def detach_current(self, window=None):
        # type: (Any) -> bool
        """Unbind the bound session. Host view stays with a placeholder."""
        window = window or self.window
        host = self._view
        if host is None:
            return False
        try:
            if not host.is_valid():
                self._view = None
                return False
        except Exception:
            self._view = None
            return False
        from core.registry import for_view

        current = for_view(host)
        if current is not None:
            self._detach_session(current)
        self._write_placeholder(host)
        return True

    def _write_placeholder(self, host):
        # type: (Any) -> None
        try:
            host.set_read_only(False)
            host.run_command(keys.CMD_CLEAR_ALL)
            host.run_command(keys.CMD_INSERT, {"pos": 0, "text": PLACEHOLDER})
            host.set_read_only(True)
        except Exception:
            pass
        try:
            host.set_name("Submarine")
        except Exception:
            pass

    def switch_to_single(self, window=None):
        # type: (Any) -> None
        """tabs → single: bind the active session; close every other sheet."""
        from core.registry import sessions_for_window

        window = window or self.window
        live = [
            s for s in sessions_for_window(window)
            if not getattr(s, "quick_mode", False)
        ]
        if not live:
            return
        bound = self._pick_bound(window, live)
        view = None
        try:
            view = bound.output.view if bound.output else None
            if view is not None and not view.is_valid():
                view = None
        except Exception:
            view = None
        if view is not None:
            self._view = view
            self._stamp_host(view)
        for s in live:
            if s is bound:
                continue
            other_view = None
            try:
                other_view = s.output.view if s.output else None
            except Exception:
                other_view = None
            self._detach_session(s)
            if other_view is None:
                continue
            try:
                if not other_view.is_valid():
                    continue
                keys.write_setting(other_view.settings(), keys.SOFT_CLOSE, True)
                other_view.close()
            except Exception:
                pass
        self.attach(window, bound, focus=True)

    def switch_to_tabs(self, window=None):
        # type: (Any) -> None
        """single → tabs: give each detached live session its own sheet."""
        from core.registry import for_view, sessions_for_window
        from ui.session_list import reveal_live_session

        window = window or self.window
        host = self._view
        bound = None
        if host is not None:
            try:
                if host.is_valid():
                    bound = for_view(host)
            except Exception:
                bound = None
        for s in sessions_for_window(window):
            if getattr(s, "quick_mode", False):
                continue
            if s is bound:
                continue
            try:
                reveal_live_session(window, s, focus=False, force_sheet=True)
            except Exception:
                pass

    def _pick_bound(self, window, live):
        # type: (Any, list) -> Any
        try:
            from ui.session_api import get_active_session
            active = get_active_session(window)
        except Exception:
            active = None
        if active is not None and active in live:
            return active
        for s in live:
            try:
                view = s.output.view if s.output else None
                if view is not None and view.is_valid():
                    return s
            except Exception:
                continue
        return live[0]


def apply_ui_mode(window, mode=None):
    # type: (Any, Optional[str]) -> None
    if window is None:
        return
    if mode is None:
        mode = ui_mode()
    hv = HostView.for_window(window)
    if mode == UI_MODE_SINGLE:
        hv.switch_to_single(window)
    else:
        hv.switch_to_tabs(window)
