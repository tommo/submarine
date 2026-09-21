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
# The no-session sheet is rendered by `ui/idle.py`; there is no placeholder
# text here any more.

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
        # Self-healing: a window that already has an output sheet never gets a
        # second one. The stamp can be lost (plugin reload, an older build, a
        # sheet adopted before the host existed), and depending on it alone
        # meant host_view() created a fresh sheet per session.
        stray = self._find_any_output(window)
        if stray is not None:
            self._stamp_host(stray)
            self._view = stray
            return stray
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

    def _find_any_output(self, window):
        # type: (Any) -> Any
        """First adoptable output view, host stamp or not.

        A torn-off session owns its own sheet and must never be adopted as the
        host, or the host would show a sheet the user detached.
        """
        if not window:
            return None
        try:
            views = list(window.views())
        except Exception:
            return None
        from core.registry import for_view
        for v in views:
            try:
                if not v.is_valid():
                    continue
                if not keys.is_output_view(v):
                    continue
                if keys.read_setting(v.settings(), keys.QUICK):
                    continue
                if keys.read_setting(v.settings(), keys.SESSION_LIST):
                    continue
                owner = for_view(v)
                if owner is not None and getattr(owner, "torn_off", False):
                    continue
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
            already = False
            try:
                av = window.active_view()
                already = av is not None and av.id() == host.id()
            except Exception:
                already = False
            if focus and not already:
                self._finish_bound(window, session, host, focus=True)
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
            # A session owns this sheet now; the idle page and its flag go away.
            clear_idle = getattr(output, "clear_idle", None)
            if callable(clear_idle):
                try:
                    clear_idle()
                except Exception:
                    pass

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

    def handoff_host_on_dismiss(self, window, session):
        # type: (Any, Any) -> bool
        """Keep the host sheet when dismissing the bound session.

        Closing Codex (or any bound session) must not destroy the session
        view if another live session still needs it. Detach first so
        a later teardown/idle cannot paint onto the host, then attach the next.
        Returns True if the sheet must stay (a peer was found). Never
        stops the outgoing session; the caller decides that.
        """
        if not is_single_mode() or session is None:
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
        if bound is not None and bound is not session:
            # Only the session painted on the host may hand it off. A
            # viewless (backgrounded) peer being dismissed must leave the
            # host on whatever the user is looking at.
            try:
                ov = session.output.view if session.output else None
                if ov is None or ov.id() != host.id():
                    return False
            except Exception:
                return False
        nxt = self._pick_next_for_host(window, session)
        if nxt is None:
            return False
        # Unused (query_count 0) peers still count. Empty is a list/history
        # filter, not a reason to destroy the host.
        # Peel the outgoing session off the host before any teardown paint.
        self._detach_session(session)
        try:
            session.backgrounded = True
        except Exception:
            pass
        try:
            self.attach(window, nxt, focus=True)
        except Exception:
            pass
        return True

    def _session_fits_window(self, session, window):
        # type: (Any, Any) -> bool
        if session is None:
            return False
        if window is None:
            return True
        try:
            from ui.session_list import belongs_to_window
            if belongs_to_window(session, window):
                return True
        except Exception:
            pass
        want = _window_key(window)
        sw = getattr(session, "window", None)
        if sw is None:
            try:
                ov = session.output.view if session.output else None
                if ov is not None:
                    sw = ov.window()
            except Exception:
                sw = None
        if sw is None:
            # Backgrounded/unused sheets often drop window. Keep them as
            # handoff candidates rather than closing the host.
            return True
        return _window_key(sw) == want

    def _pick_next_for_host(self, window, exclude):
        # type: (Any, Any) -> Any
        from core.registry import default_registry
        from ui.session_list import access_ts

        candidates = []
        for s in default_registry.iter_sessions():
            if s is exclude:
                continue
            if getattr(s, "quick_mode", False):
                continue
            if getattr(s, "torn_off", False):
                continue
            if not self._session_fits_window(s, window):
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
            # The modal keymap stamps belong to the outgoing session; the
            # next one re-stamps its own in _paint_bound.
            self._clear_modal_stamps(getattr(output, "view", None))
            try:
                output.view = None
            except Exception:
                pass

    @staticmethod
    def _clear_modal_stamps(view):
        # type: (Any) -> None
        if view is None:
            return
        try:
            if not view.is_valid():
                return
            st = view.settings()
            keys.erase_setting(st, keys.HAS_QUESTION)
            keys.erase_setting(st, keys.HAS_MODAL)
        except Exception:
            pass

    @staticmethod
    def _sync_modal_stamps(output):
        # type: (Any) -> None
        modals = getattr(output, "modals", None)
        sync = getattr(modals, "_sync_modal_settings", None)
        if callable(sync):
            try:
                sync()
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
        # A question/permission that arrived while viewless was never
        # stamped; stamp it now so the 1-4/Enter/Esc keymaps fire.
        self._sync_modal_stamps(output)
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
        """No session to bind: show the branding page (ui/idle.py).

        Was a bare `(no session)` line, which said nothing about this window,
        what a new session would start, or how to get out of the state.
        """
        try:
            from ui import idle
            idle.render(host, self.window)
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

    def settle_single_view(self, window):
        # type: (Any) -> int
        """Keep one host per window; close the other restored output sheets.

        Same shape as switch_to_single, except a torn-off sheet keeps its own
        sheet (single mode is "host view + tear-off") and nothing is retitled
        or repainted beyond the host attach.
        """
        from core.registry import sessions_for_window

        window = window or self.window
        if window is None:
            return 0
        live = [
            s for s in sessions_for_window(window)
            if not getattr(s, "quick_mode", False)
        ]
        if not live:
            return 0
        bound = self._pick_bound(window, live)
        host = self.host_view(
            window, create_via=getattr(bound, "output", None))
        if host is None:
            return 0
        host_id = None
        try:
            host_id = host.id()
        except Exception:
            host_id = None
        before = self._live_output_views(window)
        for s in live:
            if s is bound or getattr(s, "torn_off", False):
                continue
            view = None
            try:
                view = s.output.view if s.output else None
            except Exception:
                view = None
            if view is None:
                continue
            try:
                if not view.is_valid() or view.id() == host_id:
                    continue
            except Exception:
                continue
            self._detach_session(s)
            try:
                keys.write_setting(view.settings(), keys.SOFT_CLOSE, True)
                view.close()
            except Exception:
                pass
        if bound is not None:
            self.attach(window, bound, focus=True)
        self._close_orphan_output_views(window)
        return max(0, before - self._live_output_views(window))

    def _live_output_views(self, window):
        # type: (Any) -> int
        try:
            return len([
                v for v in window.views()
                if v.is_valid() and keys.is_output_view(v)
            ])
        except Exception:
            return 0

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
        if host_id is None:
            # Never sweep with no host resolved: the guard below is the only
            # thing keeping the host, so an unresolvable host closed every
            # output sheet and each new session then made a fresh one.
            try:
                resolved = self.host_view(window)
                if resolved is not None and resolved.is_valid():
                    host_id = resolved.id()
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


def claim_host_for_restore(window, session, view):
    # type: (Any, Any, Any) -> Any
    """single mode: the first restored sheet becomes the window's host.

    Every later restored output sheet is a duplicate. It must NOT claim the
    host (that left one "host" per restored sheet, so a new session landed on
    a second sheet). Its session stays detached — the session list is how it
    is reached — and the sheet is closed by settle_single_view().
    A torn-off session keeps its own sheet. Returns the host view, or None in
    tabs mode.
    """
    if not is_single_mode() or window is None or view is None:
        return None
    if getattr(session, "torn_off", False):
        return None
    output = getattr(session, "output", None)
    hv = HostView.for_window(window)
    host = hv.host_view(window, create_via=output)
    if host is None:
        return None
    try:
        if host.id() != view.id() and output is not None:
            output.view = None
    except Exception:
        pass
    return host


def settle_single_view(window):
    # type: (Any) -> int
    """Collapse duplicate output sheets onto the window's one host view.

    Startup restore reopens a sheet per previously-open session; in single
    mode only the host may stay. Torn-off and quick sheets are left alone.
    Returns the number of sheets closed.
    """
    if not is_single_mode() or window is None:
        return 0
    return HostView.for_window(window).settle_single_view(window)


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
