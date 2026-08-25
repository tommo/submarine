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
_CHROME_DEFER = True


def set_chrome_defer(on):
    # type: (bool) -> None
    """Test hook: False runs post-paint chrome inline. Production never calls this."""
    global _CHROME_DEFER
    _CHROME_DEFER = bool(on)


def _after_paint(fn):
    # type: (Any) -> None
    """Run `fn` after the swap paint. Tests with no sublime run it inline."""
    if _CHROME_DEFER and sublime is not None:
        sublime.set_timeout(fn, 0)
    else:
        fn()


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
        return UI_MODE_SINGLE
    else:
        try:
            raw = sublime.load_settings(SETTINGS_FILE).get(
                "ui_mode", UI_MODE_SINGLE)
        except Exception:
            return UI_MODE_SINGLE
    mode = (raw or UI_MODE_SINGLE)
    if isinstance(mode, str):
        mode = mode.strip().lower()
    else:
        mode = UI_MODE_SINGLE
    if mode == UI_MODE_TABS:
        return UI_MODE_TABS
    if mode == UI_MODE_SINGLE:
        return UI_MODE_SINGLE
    return UI_MODE_SINGLE


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
        global _mode_override, _CHROME_DEFER
        _INSTANCES.clear()
        _mode_override = None
        _CHROME_DEFER = True

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

        if getattr(session, "torn_off", False):
            return False

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
        self._refresh_list()
        return True

    def bound_session(self, window=None):
        # type: (Any) -> Any
        """Session currently shown on the host view, or None."""
        window = window or self.window
        host = self._view
        if host is None:
            host = self._find_existing(window)
        if host is None:
            return None
        try:
            if not host.is_valid():
                return None
        except Exception:
            return None
        from core.registry import for_view
        return for_view(host)

    def tear_off(self, window, session=None, focus=True):
        # type: (Any, Any, bool) -> bool
        """Give the bound session its own sheet. Host stays open."""
        if not is_single_mode():
            return False
        window = window or self.window
        host = self._view
        if host is None:
            host = self._find_existing(window)
        if host is None:
            return False
        try:
            if not host.is_valid():
                return False
        except Exception:
            return False
        bound = self.bound_session(window)
        if session is None:
            session = bound
        if session is None or session is not bound:
            return False
        if getattr(session, "torn_off", False):
            return True

        from ui.session_list import reveal_live_session

        self._detach_session(session)
        session.torn_off = True
        ok = reveal_live_session(window, session, focus=focus, force_sheet=True)
        snap = getattr(session, "surface", None) or {}
        output = getattr(session, "output", None)
        if output is not None and snap:
            restore = getattr(output, "surface_restore", None)
            if callable(restore):
                try:
                    restore(snap)
                except Exception:
                    pass
        nxt = self._pick_next_for_host(window, session)
        if nxt is not None:
            self.attach(window, nxt, focus=False)
        else:
            self._write_placeholder(host)
        self._refresh_list()
        return bool(ok)

    def dock(self, window, session, focus=True):
        # type: (Any, Any, bool) -> bool
        """Bind a torn-off session back to the host and close its sheet."""
        if not is_single_mode():
            return False
        if session is None or not getattr(session, "torn_off", False):
            return False
        window = window or self.window
        standalone = None
        try:
            standalone = session.output.view if session.output else None
            if standalone is not None and not standalone.is_valid():
                standalone = None
        except Exception:
            standalone = None
        session.torn_off = False
        ok = self.attach(window, session, focus=focus)
        if standalone is not None:
            try:
                bound_view = session.output.view if session.output else None
                same = False
                if bound_view is not None:
                    try:
                        same = bound_view.id() == standalone.id()
                    except Exception:
                        same = bound_view is standalone
                if not same and standalone.is_valid():
                    keys.write_setting(standalone.settings(), keys.SOFT_CLOSE, True)
                    standalone.close()
            except Exception:
                pass
        self._refresh_list()
        return bool(ok)

    def _pick_next_for_host(self, window, exclude):
        # type: (Any, Any) -> Any
        from core.registry import sessions_for_window
        from ui.session_list import access_ts

        candidates = []
        for s in sessions_for_window(window):
            if s is exclude:
                continue
            if getattr(s, "quick_mode", False):
                continue
            if getattr(s, "torn_off", False):
                continue
            candidates.append(s)
        if not candidates:
            return None
        candidates.sort(key=access_ts, reverse=True)
        return candidates[0]

    def _refresh_list(self):
        def _go():
            try:
                from ui.session_list import schedule_session_list_refresh
                schedule_session_list_refresh()
            except Exception:
                pass
        _after_paint(_go)

    def _clear_torn_off(self, window):
        # type: (Any) -> None
        from core.registry import sessions_for_window
        for s in sessions_for_window(window):
            try:
                s.torn_off = False
            except Exception:
                pass

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
        snap = getattr(session, "surface", None) or {}
        if not snap:
            snap = getattr(output, "_surface", None) or {}
        kind = None
        fast = getattr(output, "fast_paint", None)
        if callable(fast):
            try:
                kind = fast(snap)
            except Exception:
                kind = None
        if kind not in ("clean", "dirty", "fallback"):
            try:
                output.repaint_from_state()
            except Exception:
                pass
            kind = "fallback"
        try:
            backend = getattr(session, "backend", None) or "claude"
            keys.write_setting(host.settings(), keys.BACKEND, backend)
            sheet = getattr(output, "sheet", None)
            if sheet is not None and hasattr(sheet, "apply_theme"):
                sheet.apply_theme(backend)
        except Exception:
            pass
        if kind != "clean":
            restore = getattr(output, "surface_restore", None)
            if callable(restore):
                try:
                    restore(snap)
                except Exception:
                    pass
        persist = getattr(session, "_persist_view_identity", None)
        name = getattr(session, "display_name", None) or getattr(session, "name", None)

        def _stamps():
            if callable(persist):
                try:
                    persist()
                except Exception:
                    pass
            if name:
                try:
                    output.set_name(name)
                except Exception:
                    pass

        _after_paint(_stamps)

    def _finish_bound(self, window, session, host, focus=True):
        # type: (Any, Any, Any, bool) -> None
        def _chrome():
            setter = getattr(session, "_set_unread", None)
            if callable(setter):
                try:
                    setter(False)
                except Exception:
                    pass
            else:
                session.unread = False
                chrome = getattr(session, "chrome", None) or getattr(
                    session, "output", None)
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
            try:
                queued = list(getattr(session, "_queued_prompts", None) or [])
                chrome = getattr(session, "chrome", None) or getattr(
                    session, "output", None)
                if queued and chrome is not None and hasattr(chrome, "queue_chips"):
                    chrome.queue_chips(queued)
            except Exception:
                pass

        _after_paint(_chrome)
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
        self._clear_torn_off(window)
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
        self._close_orphan_output_views(window)

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
                    keys.erase_setting(host.settings(), keys.HOST)
            except Exception:
                bound = None
        self._clear_torn_off(window)
        for s in sessions_for_window(window):
            if getattr(s, "quick_mode", False):
                continue
            if s is bound:
                continue
            try:
                reveal_live_session(window, s, focus=False, force_sheet=True)
            except Exception:
                pass

    def _close_orphan_output_views(self, window):
        # type: (Any) -> None
        """Single-mode invariant: only the host output sheet stays."""
        if not window:
            return
        host = self._view
        host_id = None
        try:
            if host is not None and host.is_valid():
                host_id = host.id()
        except Exception:
            host_id = None
        try:
            views = list(window.views())
        except Exception:
            return
        for v in views:
            try:
                if not v.is_valid():
                    continue
                if not keys.is_output_view(v):
                    continue
                if keys.read_setting(v.settings(), keys.QUICK):
                    continue
                if host_id is not None and v.id() == host_id:
                    continue
                try:
                    from core.registry import for_view
                    owner = for_view(v)
                    if owner is not None and getattr(owner, "torn_off", False):
                        continue
                except Exception:
                    pass
                keys.write_setting(v.settings(), keys.SOFT_CLOSE, True)
                v.close()
            except Exception:
                continue

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


def can_tear_off(window):
    # type: (Any) -> bool
    if not is_single_mode() or window is None:
        return False
    session = HostView.for_window(window).bound_session(window)
    return session is not None and not getattr(session, "torn_off", False)


def can_dock(window, session=None):
    # type: (Any, Any) -> bool
    if not is_single_mode() or window is None:
        return False
    if session is None:
        try:
            from ui.session_api import get_session_for_view
            view = window.active_view()
            session = get_session_for_view(view) if view else None
        except Exception:
            session = None
    return bool(session is not None and getattr(session, "torn_off", False))


def tear_off_session(window, session=None, focus=True):
    # type: (Any, Any, bool) -> bool
    if not is_single_mode() or window is None:
        return False
    return HostView.for_window(window).tear_off(window, session, focus=focus)


def dock_session(window, session=None, focus=True):
    # type: (Any, Any, bool) -> bool
    if not is_single_mode() or window is None:
        return False
    if session is None:
        try:
            from ui.session_api import get_session_for_view
            view = window.active_view()
            session = get_session_for_view(view) if view else None
        except Exception:
            session = None
    if session is None:
        return False
    return HostView.for_window(window).dock(window, session, focus=focus)


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
    try:
        from ui.session_list import schedule_session_list_refresh
        schedule_session_list_refresh()
    except Exception:
        pass
